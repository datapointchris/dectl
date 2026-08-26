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
from botocore.exceptions import ClientError
from rich.markup import escape
from rich.table import Table

from dectl.config import LambdaConfig
from dectl.output import console
from dectl.output import error

# Elapsed wall-clock for a durable execution includes every suspended wait, which can run for
# months — that gap between duration and billed compute is the whole point of the feature. The
# formatter is shared with every resource that shows a duration, so the notation does not change
# between two commands read in one session.
from dectl.output import format_duration
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


# How many published versions a sweep reaches back over. Executions are keyed by version, so a
# function deployed often accumulates one bucket per deploy; the interesting ones are the newest.
VERSION_SWEEP_DEPTH = 10

# How many executions per version a tail match is looked for in, and the cap `executions --limit`
# is held to. The two are one number on purpose: a tail is typed off a listing, so anything the
# listing can print has to be inside the window the resolver searches. Let them drift and a tail
# read off a long listing either fails to resolve or, worse, resolves to one of two candidates
# because the other fell outside the window and the ambiguity was never seen.
SUFFIX_SEARCH_LIMIT = 50


def invoke_qualifier(fn: LambdaConfig) -> str:
    """The qualifier to invoke through — an alias when one is configured, else $LATEST.

    Lambda rejects an unqualified invoke of a durable function rather than defaulting it, because
    the execution is pinned to the version it starts on. An alias is the *right* thing to send
    here: it resolves to whatever version is live at that moment, which is what triggers do."""
    return fn.live_alias or '$LATEST'


def is_version(qualifier: str) -> bool:
    return qualifier.startswith('$LATEST') or qualifier.isdigit()


def listing_qualifier(client, function_name: str, qualifier: str) -> str:
    """The qualifier ListDurableExecutionsByFunction will accept — a version number or $LATEST.

    An alias name never appears in a durable execution ARN: Lambda resolves it to a version number
    at invoke time, so there is nothing for the list API to match and it refuses with "cannot
    filter durable executions by alias". (The API reference says Qualifier takes "the function
    version or alias"; for this operation that is wrong.) An alias therefore has to be resolved to
    the version it currently points at — which is also why `deploy --publish` appears to empty the
    list: it moves the alias to a new version and the previous runs stay under the old one. That
    is what sweep_executions is for."""
    if is_version(qualifier):
        return qualifier
    try:
        return client.get_alias(FunctionName=function_name, Name=qualifier)['FunctionVersion']
    except ClientError as exc:
        if exc.response.get('Error', {}).get('Code') != 'ResourceNotFoundException':
            raise
        error(f'no alias "{qualifier}" on {function_name} — pass --qualifier with a version, or $LATEST')
        raise typer.Exit(1) from exc


def epoch_millis(timestamp) -> int | None:
    """boto3 parses these into datetimes; CloudWatch Logs wants epoch milliseconds."""
    return int(timestamp.timestamp() * 1000) if timestamp is not None else None


def recent_versions(client, function_name: str, depth: int = VERSION_SWEEP_DEPTH) -> list[str]:
    """The versions a sweep covers: the newest published ones, then $LATEST.

    ListVersionsByFunction pages oldest-first, so the newest are at the end — a function published
    on every deploy has hundreds of versions and only the last few still hold executions anyone is
    looking for. $LATEST goes last because it only collects unpublished `run` invocations."""
    versions: list[str] = []
    marker = None
    while True:
        kwargs: dict[str, Any] = {'FunctionName': function_name, 'MaxItems': 50}
        if marker:
            kwargs['Marker'] = marker
        resp = client.list_versions_by_function(**kwargs)
        versions.extend(v['Version'] for v in resp.get('Versions', []))
        marker = resp.get('NextMarker')
        if not marker:
            break

    published = [version for version in versions if version != '$LATEST']
    return published[-depth:][::-1] + ['$LATEST']


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


def sweep_executions(
    client,
    function_name: str,
    limit: int = 10,
    status: str | None = None,
    name: str | None = None,
    depth: int = VERSION_SWEEP_DEPTH,
) -> tuple[list[dict], list[str]]:
    """List executions across recent versions, merged newest-first, with the versions scanned.

    One list call per version is the only way to span them, since the API filters by exactly one
    qualifier. Returning the scanned versions lets the caller say how far back it actually looked
    rather than presenting a bounded search as an exhaustive one."""
    versions = recent_versions(client, function_name, depth=depth)
    found: list[dict] = []
    for version in versions:
        found.extend(list_executions(client, function_name, version, limit=limit, status=status, name=name))
    # Sorted on the epoch value, not the datetime: boto3 returns timezone-aware timestamps and a
    # naive fallback for the missing case would raise rather than sort.
    found.sort(key=started_at, reverse=True)
    return found[:limit], versions


def started_at(execution: dict) -> float:
    started = execution.get('StartTimestamp')
    return started.timestamp() if started is not None else 0.0


def resolve_execution(client, function_name: str, qualifier: str, execution: str | None = None) -> dict:
    """Resolve the execution argument — an ARN, a name, a unique tail of a name, or nothing.

    `run --name` gives an execution a name you can retype. Without one Lambda assigns a UUID, so
    the handle is its tail: `cfd01dc3a122` reaches it, and so does any suffix long enough to be
    unique. Omitting the argument takes the most recent execution, matching `glue logs` and
    `sfn logs`.

    The tail is tried last, after both exact-name paths, so an execution whose full name happens
    to end another's still wins itself. A tail matching several is an error naming them, never a
    guess at which one was meant.

    A miss falls back to sweeping recent versions before giving up, because the version a name
    ran under is not something you can be expected to know — and after a `deploy --publish` it is
    no longer the one the alias points at."""
    if execution and execution.startswith('arn:'):
        return client.get_durable_execution(DurableExecutionArn=execution)

    matches = list_executions(client, function_name, qualifier, limit=1, name=execution)
    if not matches:
        matches, scanned = sweep_executions(client, function_name, limit=1, name=execution)
        if matches:
            info(f'found under version {version_of(matches[0])}')
        elif execution:
            matches = [execution_by_suffix(client, function_name, execution, scanned)]
        else:
            error(f'no durable execution for {function_name} in versions {", ".join(scanned)}')
            raise typer.Exit(1)
    return client.get_durable_execution(DurableExecutionArn=matches[0]['DurableExecutionArn'])


def execution_by_suffix(client, function_name: str, suffix: str, scanned: list[str]) -> dict:
    """The one execution whose name ends with `suffix`, across the versions already swept.

    Matched on the tail rather than the head because Lambda's generated names are UUIDs: a
    prefix of one carries the timestamp and little else, while the tail is where the entropy is.
    Several matches is a usage error listing the candidates — picking the newest would resolve
    silently to something the caller did not name."""
    candidates = [
        found
        for version in scanned
        for found in list_executions(client, function_name, version, limit=SUFFIX_SEARCH_LIMIT)
        if str(found.get('DurableExecutionName', '')).endswith(suffix)
    ]
    if not candidates:
        error(f'no durable execution named {suffix}, or ending in it')
        error(f'searched the {SUFFIX_SEARCH_LIMIT} most recent on each of versions {", ".join(scanned)}')
        error('an older execution has to be named in full, or by its ARN')
        raise typer.Exit(1)
    if len(candidates) > 1:
        error(f'{suffix} matches {len(candidates)} executions; use more of the name:')
        for found in sorted(candidates, key=started_at, reverse=True):
            error(f'  {found.get("DurableExecutionName", "")} ({version_of(found)})')
        raise typer.Exit(2)
    return candidates[0]


def version_of(execution: dict) -> str:
    """The version an execution ran on, read back out of its ARN.

    The ARN is `...:function:<name>:<version>/durable-execution/<id>/<id>`, and the version in it
    is always a number — the alias it was invoked through is already resolved away by this point,
    which is the whole reason listing by alias does not work."""
    arn = execution.get('DurableExecutionArn', '')
    head = arn.split('/durable-execution/')[0]
    return head.rsplit(':', 1)[-1] if ':' in head else ''


def colored_status(status: str) -> str:
    color = EXECUTION_STATUS_COLORS.get(status, 'white')
    return f'[{color}]{status}[/{color}]'


def execution_to_dict(execution: dict) -> dict:
    return {
        'name': execution.get('DurableExecutionName'),
        'status': execution.get('Status'),
        'version': version_of(execution),
        'started': execution.get('StartTimestamp'),
        'ended': execution.get('EndTimestamp'),
        'arn': execution.get('DurableExecutionArn'),
    }


def render_executions_table(alias: str, scope: str, executions: list[dict]) -> None:
    """Executions with the version each ran on, since that is what scopes the list.

    scope names what was searched — an alias and the version it resolved to, or the span of a
    sweep — so a short list reads as "this is where I looked" rather than "this is all there is".

    The name folds rather than truncating, because `history` and `logs` accept its tail as a short
    handle and an ellipsis in the middle of a UUID hides exactly the end you would retype."""
    table = Table(title=f'{alias} durable executions ({scope})')
    table.add_column('name', overflow='fold')
    table.add_column('status')
    table.add_column('version')
    table.add_column('started')
    table.add_column('duration')
    for execution in executions:
        started = execution.get('StartTimestamp')
        table.add_row(
            execution.get('DurableExecutionName', ''),
            colored_status(execution.get('Status', '')),
            version_of(execution),
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
