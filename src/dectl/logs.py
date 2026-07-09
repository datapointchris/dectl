import time

from dectl.output import console
from dectl.output import info

GLUE_OUTPUT_LOG_GROUP = '/aws-glue/python-jobs/output'
GLUE_ERROR_LOG_GROUP = '/aws-glue/python-jobs/error'


def stream_prefix(log_group: str) -> str:
    """Tag each event with its source stream so a line duplicated across the
    output and error groups (a propagating logger in the job) is obvious."""
    if log_group == GLUE_ERROR_LOG_GROUP:
        return '[red]err[/red] '
    return '[cyan]out[/cyan] '


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
            console.print(event['message'].rstrip())

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
                console.print(f'{stream_prefix(log_group)}{event["message"].rstrip()}')
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
