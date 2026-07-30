import json
import re
import time

from rich.markup import escape
from rich.padding import Padding
from rich.syntax import Syntax

from dectl.output import console
from dectl.output import info

GLUE_OUTPUT_LOG_GROUP = '/aws-glue/python-jobs/output'
GLUE_ERROR_LOG_GROUP = '/aws-glue/python-jobs/error'

TIMESTAMP_KEYS = ('timestamp', 'asctime', 'time')
LEVEL_KEYS = ('level', 'levelname', 'severity')
MESSAGE_KEYS = ('message', 'msg', 'event')
TRACEBACK_KEYS = ('exc_info', 'exception', 'stack_trace', 'stacktrace', 'traceback')
HEADER_KEYS = frozenset(TIMESTAMP_KEYS + LEVEL_KEYS + MESSAGE_KEYS)

MARKUP_TAG = re.compile(r'\[/?[^\[\]]+\]')

LEVEL_COLORS = {
    'CRITICAL': 'red',
    'ERROR': 'red',
    'WARNING': 'yellow',
    'WARN': 'yellow',
    'INFO': 'green',
    'DEBUG': 'blue',
}


def stream_prefix(log_group: str) -> str:
    """Tag only the error group. A well-configured job logs through one stdout
    handler, so stderr is the exceptional case (tracebacks, the warnings module,
    a library writing direct) and worth marking; labelling every ordinary line
    would spend width on a constant. A line tagged err that also appears
    untagged means the job has a duplicate handler."""
    if log_group == GLUE_ERROR_LOG_GROUP:
        return '[red]err[/red] '
    return ''


def visible_width(markup: str) -> int:
    """Width of rich markup once the tags are stripped, for aligning continuation lines."""
    return len(MARKUP_TAG.sub('', markup))


def first_value(data: dict, keys: tuple[str, ...]) -> str:
    for key in keys:
        if data.get(key) not in (None, ''):
            return str(data[key])
    return ''


def render_event(message: str, prefix: str = '') -> None:
    """Print a CloudWatch event, restructuring it when the whole message is a
    JSON object. Structured logs (durable functions, python-json-logger) arrive
    as one dense line with any traceback collapsed into a single \\n-escaped
    field; this lifts the common fields into a header and re-expands tracebacks
    into readable, syntax-highlighted frames. Anything else prints verbatim."""
    text = message.rstrip()
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        console.print(f'{prefix}{escape(text)}')
        return
    if not isinstance(data, dict):
        console.print(f'{prefix}{escape(text)}')
        return

    timestamp = first_value(data, TIMESTAMP_KEYS)
    level = first_value(data, LEVEL_KEYS)
    body = first_value(data, MESSAGE_KEYS)
    level_color = LEVEL_COLORS.get(level.upper(), 'white')

    header_parts = []
    if timestamp:
        header_parts.append(f'[cyan]{escape(timestamp)}[/cyan]')
    if level:
        header_parts.append(f'[bold {level_color}]{escape(level)}[/bold {level_color}]')
    if body:
        header_parts.append(escape(body))
    console.print(f'{prefix}{" ".join(header_parts)}' if header_parts else f'{prefix}{escape(text)}')

    # Hang the expanded fields under the header rather than at column 0, so a prefixed
    # record reads as one block instead of a labelled line followed by orphans.
    indent_width = visible_width(prefix)
    indent = ' ' * indent_width

    for key, value in data.items():
        if key in HEADER_KEYS:
            continue
        if key.lower() in TRACEBACK_KEYS and isinstance(value, str) and value.strip():
            frames = Syntax(value, 'pytb', theme='ansi_dark', word_wrap=True)
            console.print(Padding(frames, (0, 0, 0, indent_width)))
        else:
            console.print(f'{indent}  [bold]{escape(key)}[/bold]: {escape(str(value))}')


# Execution-history event types that mean the state machine run is over, so tailing can stop.
TERMINAL_EXECUTION_EVENTS = frozenset({'ExecutionSucceeded', 'ExecutionFailed', 'ExecutionAborted', 'ExecutionTimedOut'})


def render_history_event(event: dict) -> None:
    """Print one Step Functions execution-history event as a readable, colored line.

    Each event carries exactly one '<something>EventDetails' dict; the interesting fields
    across all of them are a state/resource name and, on failures, error/cause. Pull those
    generically rather than special-casing all ~40 event types."""
    event_type = event.get('type', 'Unknown')
    timestamp = event.get('timestamp')
    if timestamp is not None and hasattr(timestamp, 'isoformat'):
        stamp = timestamp.isoformat()
    else:
        stamp = str(timestamp)

    details: dict = {}
    for key, value in event.items():
        if key.endswith('EventDetails') and isinstance(value, dict):
            details = value
            break

    lowered = event_type.lower()
    if any(word in lowered for word in ('failed', 'aborted', 'timedout')):
        color = 'red'
    elif 'succeeded' in lowered:
        color = 'green'
    elif any(word in lowered for word in ('entered', 'started', 'scheduled')):
        color = 'cyan'
    else:
        color = 'white'

    label = details.get('name') or details.get('resource') or ''
    header = [f'[cyan]{escape(stamp)}[/cyan]', f'[bold {color}]{escape(event_type)}[/bold {color}]']
    if label:
        header.append(escape(label))
    console.print(' '.join(header))

    if details.get('error'):
        console.print(f'  [bold red]error[/bold red]: {escape(str(details["error"]))}')
    if details.get('cause'):
        console.print(f'  [bold red]cause[/bold red]: {escape(str(details["cause"]))}')


def tail_execution_history(sfn_client, execution_arn: str, follow: bool = True) -> None:
    """Poll a state machine execution's history until it reaches a terminal state.

    Event ids are stable and unique within an execution, so dedup by id; get_execution_history
    returns the full history each call in chronological order."""
    seen_event_ids: set = set()
    while True:
        events = []
        next_token = None
        while True:
            kwargs: dict = {'executionArn': execution_arn}
            if next_token:
                kwargs['nextToken'] = next_token
            resp = sfn_client.get_execution_history(**kwargs)
            events.extend(resp.get('events', []))
            next_token = resp.get('nextToken')
            if not next_token:
                break

        for event in events:
            if event['id'] in seen_event_ids:
                continue
            render_history_event(event)
            seen_event_ids.add(event['id'])

        if not follow or any(event.get('type') in TERMINAL_EXECUTION_EVENTS for event in events):
            break
        time.sleep(2)


def wait_for_log_stream(logs_client, log_group: str, stream_prefix: str, timeout: int = 120) -> str | None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        resp = logs_client.describe_log_streams(
            logGroupName=log_group,
            logStreamNamePrefix=stream_prefix,
            limit=1,
        )
        streams = resp.get('logStreams', [])
        if streams:
            return streams[0]['logStreamName']
        time.sleep(5)
    return None


def tail_log_groups(logs_client, sources: list[tuple[str, str]], follow: bool = True) -> None:
    """Tail several CloudWatch log groups into one time-ordered stream.

    sources is a list of (prefix, log_group). Each poll fetches new events from every group,
    merges them, and renders in timestamp order so the cross-source causal sequence (lambda A
    fires -> state machine transitions -> lambda B fires) reads top to bottom. Uses the same
    moving-startTime + boundary dedup as the single-group Lambda tailer, per group."""
    now = int(time.time() * 1000)
    prefixes = {group: prefix for prefix, group in sources}
    start_times = {group: now for group in prefixes}
    seen_at_boundary: dict[str, set] = {group: set() for group in prefixes}

    while True:
        batch = []
        for group in prefixes:
            fetched = []
            next_token = None
            while True:
                kwargs: dict = {'logGroupName': group, 'startTime': start_times[group]}
                if next_token:
                    kwargs['nextToken'] = next_token
                page = logs_client.filter_log_events(**kwargs)
                fetched.extend(page.get('events', []))
                next_token = page.get('nextToken')
                if not next_token:
                    break

            new_events = [event for event in fetched if event['eventId'] not in seen_at_boundary[group]]
            for event in new_events:
                batch.append((event['timestamp'], prefixes[group], event['message']))

            if new_events:
                max_timestamp = max(event['timestamp'] for event in new_events)
                start_times[group] = max_timestamp
                seen_at_boundary[group] = {event['eventId'] for event in fetched if event['timestamp'] == max_timestamp}

        for event in sorted(batch):
            render_event(event[2], prefix=event[1])

        if not follow:
            break
        if not batch:
            time.sleep(2)


def tail_glue_run(logs_client, run_id: str, follow: bool = True) -> None:
    info(f'waiting for log streams for run {run_id}...')

    output_stream = wait_for_log_stream(logs_client, GLUE_OUTPUT_LOG_GROUP, run_id)
    error_stream = wait_for_log_stream(logs_client, GLUE_ERROR_LOG_GROUP, run_id)

    if not output_stream and not error_stream:
        console.print('[yellow]no log streams found[/yellow]')
        return

    streams = []
    if output_stream:
        info(f'tailing {GLUE_OUTPUT_LOG_GROUP}/{output_stream}')
        streams.append((GLUE_OUTPUT_LOG_GROUP, output_stream))
    if error_stream:
        info(f'tailing {GLUE_ERROR_LOG_GROUP}/{error_stream}')
        streams.append((GLUE_ERROR_LOG_GROUP, error_stream))

    tokens = {s: None for s in streams}

    while True:
        got_events = False
        for log_group, stream_name in streams:
            kwargs: dict = {
                'logGroupName': log_group,
                'logStreamName': stream_name,
                'startFromHead': True,
            }
            token = tokens[(log_group, stream_name)]
            if token:
                kwargs['nextToken'] = token
                kwargs.pop('startFromHead', None)

            resp = logs_client.get_log_events(**kwargs)
            for event in resp.get('events', []):
                render_event(event['message'], prefix=stream_prefix(log_group))
                got_events = True

            tokens[(log_group, stream_name)] = resp.get('nextForwardToken')

        if not got_events:
            if not follow:
                break
            time.sleep(2)


def tail_lambda_logs(logs_client, function_name: str, follow: bool = True) -> None:
    log_group = f'/aws/lambda/{function_name}'
    info(f'tailing {log_group}')

    # Lambda writes each execution environment to its own log stream, and an invocation that
    # cold-starts (after the previous warm environment is reaped, ~5-15 min idle) lands in a
    # brand-new stream. Following a single stream would silently miss those later runs, so poll
    # the whole log group by time with filter_log_events, which spans every stream.
    stream_resp = logs_client.describe_log_streams(
        logGroupName=log_group,
        orderBy='LastEventTime',
        descending=True,
        limit=1,
    )
    streams = stream_resp.get('logStreams', [])
    # Seed from the newest stream's first event so existing output for the current run shows
    # before we start following; fall back to now when the function has never run.
    start_time = streams[0].get('firstEventTimestamp') if streams else None
    if start_time is None:
        start_time = int(time.time() * 1000)

    # eventIds already rendered at exactly start_time. filter_log_events treats startTime as
    # inclusive, so those events come back on the next poll and must be skipped to avoid dupes.
    seen_at_boundary: set[str] = set()
    while True:
        fetched = []
        next_token = None
        while True:
            kwargs: dict = {'logGroupName': log_group, 'startTime': start_time}
            if next_token:
                kwargs['nextToken'] = next_token
            page = logs_client.filter_log_events(**kwargs)
            fetched.extend(page.get('events', []))
            next_token = page.get('nextToken')
            if not next_token:
                break

        new_events = [event for event in fetched if event['eventId'] not in seen_at_boundary]
        for event in new_events:
            render_event(event['message'])

        if new_events:
            max_timestamp = max(event['timestamp'] for event in new_events)
            start_time = max_timestamp
            seen_at_boundary = {event['eventId'] for event in fetched if event['timestamp'] == max_timestamp}
        else:
            if not follow:
                break
            time.sleep(2)
