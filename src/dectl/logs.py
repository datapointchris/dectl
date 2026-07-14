import json
import time

from rich.markup import escape
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

LEVEL_COLORS = {
    'CRITICAL': 'red',
    'ERROR': 'red',
    'WARNING': 'yellow',
    'WARN': 'yellow',
    'INFO': 'green',
    'DEBUG': 'blue',
}


def stream_prefix(log_group: str) -> str:
    """Tag each event with its source stream so a line duplicated across the
    output and error groups (a propagating logger in the job) is obvious."""
    if log_group == GLUE_ERROR_LOG_GROUP:
        return '[red]err[/red] '
    return '[cyan]out[/cyan] '


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

    for key, value in data.items():
        if key in HEADER_KEYS:
            continue
        if key.lower() in TRACEBACK_KEYS and isinstance(value, str) and value.strip():
            console.print(Syntax(value, 'pytb', theme='ansi_dark', word_wrap=True))
        else:
            console.print(f'  [bold]{escape(key)}[/bold]: {escape(str(value))}')


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
