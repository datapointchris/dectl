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


def tail_log_stream(logs_client, log_group: str, stream_name: str, follow: bool = True) -> None:
    token = None
    while True:
        kwargs: dict = {
            'logGroupName': log_group,
            'logStreamName': stream_name,
            'startFromHead': True,
        }
        if token:
            kwargs['nextToken'] = token
            kwargs.pop('startFromHead', None)

        resp = logs_client.get_log_events(**kwargs)
        for event in resp.get('events', []):
            render_event(event['message'])

        new_token = resp.get('nextForwardToken')
        if new_token == token:
            if not follow:
                break
            time.sleep(2)
        token = new_token


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

    resp = logs_client.describe_log_streams(
        logGroupName=log_group,
        orderBy='LastEventTime',
        descending=True,
        limit=1,
    )
    streams = resp.get('logStreams', [])
    if not streams:
        console.print('[yellow]no log streams found[/yellow]')
        return

    tail_log_stream(logs_client, log_group, streams[0]['logStreamName'], follow=follow)
