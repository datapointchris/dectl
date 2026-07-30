"""Lambda durable functions: executions, their step history, and their logger output.

A durable function's unit of work is the *execution*, not the invocation — one execution
checkpoints its way across many invocations, and can suspend for up to a year between them. That
is why none of the ordinary Lambda views answer "did this run succeed": Invocations counts
replays, Errors counts invocation faults, and a log stream holds interleaved fragments of
whichever executions that environment happened to serve.

The three things worth looking at map onto three APIs, which is what this module wraps:
executions (ListDurableExecutionsByFunction), one execution's steps and waits
(GetDurableExecutionHistory), and one execution's logger output (CloudWatch, filtered by the
execution ARN that the SDK logger stamps on every record).
"""

import time
from typing import Any

import typer
from rich.markup import escape
from rich.table import Table

from dectl.config import LambdaConfig
from dectl.output import console
from dectl.output import error
from dectl.output import info

EXECUTION_STATUS_COLORS = {
    'RUNNING': 'cyan',
    'SUCCEEDED': 'green',
    'FAILED': 'red',
    'TIMED_OUT': 'red',
    'STOPPED': 'red',
}
EXECUTION_STATUSES = tuple(EXECUTION_STATUS_COLORS)

TERMINAL_HISTORY_EVENTS = frozenset({'ExecutionSucceeded', 'ExecutionFailed', 'ExecutionTimedOut', 'ExecutionStopped'})


def qualifier_for(fn: LambdaConfig) -> str:
    """The version or alias a durable function's executions belong to.

    Durable executions are pinned to the version they started on, so both invoking and listing are
    always qualified — Lambda rejects an unqualified identifier for a durable function rather than
    defaulting it. The live alias is the right default because that is what triggers actually
    invoke; without one, executions live under $LATEST."""
    return fn.live_alias or '$LATEST'


def epoch_millis(timestamp) -> int | None:
    """boto3 parses these into datetimes; CloudWatch Logs wants epoch milliseconds."""
    return int(timestamp.timestamp() * 1000) if timestamp is not None else None


def list_executions(
    client,
    function_name: str,
    qualifier: str,
    limit: int = 10,
    status: str | None = None,
    name: str | None = None,
) -> list[dict]:
    kwargs: dict[str, Any] = {'FunctionName': function_name, 'Qualifier': qualifier, 'MaxItems': limit}
    if status:
        kwargs['Statuses'] = [status]
    if name:
        kwargs['DurableExecutionName'] = name
    return client.list_durable_executions_by_function(**kwargs).get('DurableExecutions', [])


def resolve_execution(client, function_name: str, qualifier: str, execution: str | None = None) -> dict:
    """Resolve the execution argument — an ARN, a name, or nothing — to its full description.

    Names are what you actually have to hand: `run --name` sets one for idempotency, and it is
    what the console lists. Omitting the argument takes the most recent execution, matching
    `glue logs` and `sfn logs`."""
    if execution and execution.startswith('arn:'):
        return client.get_durable_execution(DurableExecutionArn=execution)

    matches = list_executions(client, function_name, qualifier, limit=1, name=execution)
    if not matches:
        scope = f'named {execution}' if execution else f'for {function_name}:{qualifier}'
        error(f'no durable execution {scope}')
        raise typer.Exit(1)
    return client.get_durable_execution(DurableExecutionArn=matches[0]['DurableExecutionArn'])


def colored_status(status: str) -> str:
    color = EXECUTION_STATUS_COLORS.get(status, 'white')
    return f'[{color}]{status}[/{color}]'


def format_duration(start, end) -> str:
    """Elapsed wall-clock time, which for a durable execution includes every suspended wait —
    that gap between duration and billed compute is the whole point of the feature."""
    if start is None or end is None:
        return ''
    seconds = (end - start).total_seconds()
    if seconds < 60:
        return f'{seconds:.1f}s'
    minutes, seconds = divmod(int(seconds), 60)
    hours, minutes = divmod(minutes, 60)
    return f'{hours}h{minutes:02d}m' if hours else f'{minutes}m{seconds:02d}s'


def execution_to_dict(execution: dict) -> dict:
    return {
        'name': execution.get('DurableExecutionName'),
        'status': execution.get('Status'),
        'started': execution.get('StartTimestamp'),
        'ended': execution.get('EndTimestamp'),
        'arn': execution.get('DurableExecutionArn'),
    }


def render_executions_table(alias: str, qualifier: str, executions: list[dict]) -> None:
    table = Table(title=f'{alias} durable executions ({qualifier})')
    table.add_column('name')
    table.add_column('status')
    table.add_column('started')
    table.add_column('duration')
    for execution in executions:
        started = execution.get('StartTimestamp')
        table.add_row(
            execution.get('DurableExecutionName', ''),
            colored_status(execution.get('Status', '')),
            started.isoformat() if started else '',
            format_duration(started, execution.get('EndTimestamp')),
        )
    console.print(table)


def render_error_payload(payload: dict, indent: str = '  ') -> None:
    """Render the Error shape the history reuses for every kind of failure.

    The stack trace arrives as a list of frame strings rather than one blob, so it is printed
    line by line instead of being squashed back into a single field."""
    message = payload.get('ErrorMessage', '')
    error_type = payload.get('ErrorType', '')
    console.print(f'{indent}[bold red]error[/bold red]: {escape(error_type)} {escape(message)}'.rstrip())
    if payload.get('ErrorData'):
        console.print(f'{indent}[bold]data[/bold]: {escape(str(payload["ErrorData"]))}')
    for frame in payload.get('StackTrace', []):
        console.print(f'{indent}  {escape(str(frame))}')


def event_color(event_type: str) -> str:
    lowered = event_type.lower()
    if any(word in lowered for word in ('failed', 'timedout', 'stopped', 'cancelled')):
        return 'red'
    if 'succeeded' in lowered:
        return 'green'
    if any(word in lowered for word in ('started', 'scheduled')):
        return 'cyan'
    return 'white'


def truncated_payload(body: dict) -> str:
    payload = body.get('Payload', '')
    suffix = ' [yellow](truncated)[/yellow]' if body.get('Truncated') else ''
    return f'{escape(str(payload))}{suffix}'


def render_durable_event(event: dict) -> None:
    """Print one durable execution history event.

    Each event carries exactly one '<EventType>Details' member out of ~25, and they share a small
    vocabulary of shapes — Error, Result, Input, RetryDetails — so those are pulled generically
    rather than special-casing every type."""
    event_type = event.get('EventType', 'Unknown')
    timestamp = event.get('EventTimestamp')
    if timestamp is not None and hasattr(timestamp, 'isoformat'):
        stamp = timestamp.isoformat()
    else:
        stamp = str(timestamp)

    details: dict = {}
    for key, value in event.items():
        if key.endswith('Details') and key != 'RetryDetails' and isinstance(value, dict):
            details = value
            break

    color = event_color(event_type)
    header = [f'[cyan]{escape(stamp)}[/cyan]', f'[bold {color}]{escape(event_type)}[/bold {color}]']
    if event.get('Name'):
        header.append(escape(str(event['Name'])))
    console.print(' '.join(header))

    if isinstance(details.get('Error'), dict):
        render_error_payload(details['Error'].get('Payload', {}))
    if isinstance(details.get('Result'), dict):
        console.print(f'  [bold]result[/bold]: {truncated_payload(details["Result"])}')
    if isinstance(details.get('Input'), dict):
        console.print(f'  [bold]input[/bold]: {truncated_payload(details["Input"])}')

    retry = details.get('RetryDetails')
    if isinstance(retry, dict) and retry.get('CurrentAttempt', 1) > 1:
        console.print(f'  [bold]attempt[/bold]: {retry["CurrentAttempt"]}')
    if details.get('Duration') is not None:
        console.print(f'  [bold]wait[/bold]: {details["Duration"]}s')
    if details.get('FunctionName'):
        console.print(f'  [bold]function[/bold]: {escape(str(details["FunctionName"]))}')


def fetch_history(client, execution_arn: str, include_data: bool = True) -> list[dict]:
    events: list[dict] = []
    marker = None
    while True:
        kwargs: dict[str, Any] = {'DurableExecutionArn': execution_arn, 'IncludeExecutionData': include_data}
        if marker:
            kwargs['Marker'] = marker
        resp = client.get_durable_execution_history(**kwargs)
        events.extend(resp.get('Events', []))
        marker = resp.get('NextMarker')
        if not marker:
            return events


def tail_durable_history(client, execution_arn: str, follow: bool = False, include_data: bool = True) -> None:
    """Print an execution's history, optionally polling until it reaches a terminal event.

    EventIds are unique and stable within an execution, so a re-fetch is deduped by id the same
    way the Step Functions history tailer works."""
    seen_event_ids: set = set()
    while True:
        events = fetch_history(client, execution_arn, include_data=include_data)
        for event in events:
            if event.get('EventId') in seen_event_ids:
                continue
            render_durable_event(event)
            seen_event_ids.add(event.get('EventId'))

        if not follow or any(event.get('EventType') in TERMINAL_HISTORY_EVENTS for event in events):
            return
        time.sleep(2)


def render_execution_header(execution: dict) -> None:
    """One-line orientation before a history or log dump: which execution, and how it ended."""
    status = execution.get('Status', '')
    duration = format_duration(execution.get('StartTimestamp'), execution.get('EndTimestamp'))
    info(f'{execution.get("DurableExecutionName", "")} {colored_status(status)} {duration}'.rstrip())
    if isinstance(execution.get('Error'), dict):
        render_error_payload(execution['Error'])
