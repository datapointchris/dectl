import json
from datetime import datetime

import pytest

from dectl.logs import GLUE_ERROR_LOG_GROUP
from dectl.logs import GLUE_OUTPUT_LOG_GROUP
from dectl.logs import render_event
from dectl.logs import render_history_event
from dectl.logs import stream_prefix
from dectl.logs import tail_execution_history
from dectl.logs import tail_lambda_logs
from dectl.logs import tail_log_groups
from dectl.output import console


def capture_event(message: str, prefix: str = '') -> str:
    with console.capture() as capture:
        render_event(message, prefix=prefix)
    return capture.get()


class FakeLogsClient:
    """Minimal CloudWatch Logs stand-in returning queued filter_log_events pages."""

    def __init__(self, streams: list[dict], filter_pages: list[dict]) -> None:
        self.streams = streams
        self.filter_pages = filter_pages
        self.filter_calls: list[dict] = []

    def describe_log_streams(self, **kwargs) -> dict:
        return {'logStreams': self.streams}

    def filter_log_events(self, **kwargs) -> dict:
        self.filter_calls.append(kwargs)
        if self.filter_pages:
            return self.filter_pages.pop(0)
        return {'events': []}


class FakeSfnClient:
    """Step Functions stand-in returning queued get_execution_history pages."""

    def __init__(self, history_pages: list[dict]) -> None:
        self.history_pages = history_pages
        self.history_calls: list[dict] = []

    def get_execution_history(self, **kwargs) -> dict:
        self.history_calls.append(kwargs)
        if self.history_pages:
            return self.history_pages.pop(0)
        return {'events': []}


def capture_history_event(event: dict) -> str:
    with console.capture() as capture:
        render_history_event(event)
    return capture.get()


def test_glue_log_groups_are_correct():
    assert GLUE_OUTPUT_LOG_GROUP == '/aws-glue/python-jobs/output'
    assert GLUE_ERROR_LOG_GROUP == '/aws-glue/python-jobs/error'


def test_stream_prefix_tags_error_group():
    assert stream_prefix(GLUE_ERROR_LOG_GROUP) == '[red]err[/red] '


def test_stream_prefix_leaves_output_group_untagged():
    assert stream_prefix(GLUE_OUTPUT_LOG_GROUP) == ''


def test_render_event_hangs_expanded_fields_under_a_prefixed_header():
    message = json.dumps({'level': 'ERROR', 'message': 'boom', 'object_key': 'raw/x.parquet'})
    header, field = capture_event(message, prefix=stream_prefix(GLUE_ERROR_LOG_GROUP)).splitlines()[:2]
    assert header.startswith('err ')
    assert field.startswith(' ' * len('err '))


def test_render_event_passes_plain_text_through():
    assert capture_event('INFO starting job').strip() == 'INFO starting job'


def test_render_event_does_not_interpret_brackets_as_markup():
    output = capture_event('processing [batch 1] of items')
    assert '[batch 1]' in output


def test_render_event_lifts_json_fields_into_header():
    message = json.dumps({'timestamp': '2026-07-08T12:00:00', 'level': 'INFO', 'message': 'job done'})
    output = capture_event(message)
    assert 'INFO' in output
    assert 'job done' in output
    assert '2026-07-08T12:00:00' in output
    assert '{' not in output


def test_render_event_prepends_stream_prefix_to_json_header():
    message = json.dumps({'level': 'ERROR', 'message': 'boom'})
    output = capture_event(message, prefix=stream_prefix(GLUE_ERROR_LOG_GROUP))
    assert output.startswith('err ')


def test_render_event_expands_collapsed_traceback():
    traceback = 'Traceback (most recent call last):\n  File "job.py", line 3\n    raise ValueError("x")\nValueError: x'
    message = json.dumps({'level': 'ERROR', 'message': 'failed', 'exc_info': traceback})
    output = capture_event(message)
    assert 'Traceback (most recent call last):' in output
    assert 'ValueError: x' in output
    # the \n-escaped field is re-expanded onto multiple lines, not one
    assert output.count('\n') >= 4


def test_render_event_prints_extra_fields():
    message = json.dumps({'level': 'INFO', 'message': 'row', 'record_count': 42})
    output = capture_event(message)
    assert 'record_count' in output
    assert '42' in output


def test_lambda_tail_picks_up_events_across_streams():
    # The bug: a later invocation cold-starts into a new log stream. filter_log_events spans
    # the whole group, so events from both streams surface in a single tail session.
    client = FakeLogsClient(
        streams=[{'logStreamName': 'old-stream', 'firstEventTimestamp': 1000}],
        filter_pages=[
            {
                'events': [
                    {'eventId': 'a', 'timestamp': 2000, 'message': 'warm run', 'logStreamName': 'stream-A'},
                    {'eventId': 'b', 'timestamp': 3000, 'message': 'cold-start run', 'logStreamName': 'stream-B'},
                ]
            }
        ],
    )
    with console.capture() as capture:
        tail_lambda_logs(client, 'my-func', follow=False)
    output = capture.get()

    assert 'warm run' in output
    assert 'cold-start run' in output


def test_lambda_tail_does_not_reprint_boundary_events():
    # startTime is inclusive, so the newest event is re-returned on the next poll; it must not
    # print twice. A raising sleep breaks out of the follow loop once the pages drain.
    class StopTail(Exception):
        pass

    client = FakeLogsClient(
        streams=[{'logStreamName': 'old-stream', 'firstEventTimestamp': 1000}],
        filter_pages=[
            {'events': [{'eventId': 'a', 'timestamp': 2000, 'message': 'event-one', 'logStreamName': 's'}]},
            {
                'events': [
                    {'eventId': 'a', 'timestamp': 2000, 'message': 'event-one', 'logStreamName': 's'},
                    {'eventId': 'b', 'timestamp': 2001, 'message': 'event-two', 'logStreamName': 's'},
                ]
            },
        ],
    )

    def stop_sleep(_seconds):
        raise StopTail

    with pytest.MonkeyPatch.context() as monkeypatch, console.capture() as capture:
        monkeypatch.setattr('dectl.logs.time.sleep', stop_sleep)
        with pytest.raises(StopTail):
            tail_lambda_logs(client, 'my-func', follow=True)
    output = capture.get()

    assert output.count('event-one') == 1
    assert output.count('event-two') == 1


def test_render_history_event_shows_type_and_state_name():
    event = {
        'type': 'TaskStateEntered',
        'timestamp': datetime(2026, 7, 13, 12),
        'stateEnteredEventDetails': {'name': 'Transform'},
    }
    output = capture_history_event(event)
    assert 'TaskStateEntered' in output
    assert 'Transform' in output


def test_render_history_event_surfaces_error_and_cause():
    event = {
        'type': 'ExecutionFailed',
        'timestamp': datetime(2026, 7, 13, 12),
        'executionFailedEventDetails': {'error': 'States.TaskFailed', 'cause': 'lambda blew up'},
    }
    output = capture_history_event(event)
    assert 'ExecutionFailed' in output
    assert 'States.TaskFailed' in output
    assert 'lambda blew up' in output


def test_tail_execution_history_stops_at_terminal_event():
    timestamp = datetime(2026, 7, 13, 12)
    events = [
        {'id': 1, 'type': 'ExecutionStarted', 'timestamp': timestamp},
        {'id': 2, 'type': 'TaskStateEntered', 'timestamp': timestamp, 'stateEnteredEventDetails': {'name': 'Do'}},
        {'id': 3, 'type': 'ExecutionSucceeded', 'timestamp': timestamp},
    ]
    client = FakeSfnClient([{'events': events}])
    with console.capture() as capture:
        tail_execution_history(client, 'arn:execution', follow=True)
    output = capture.get()

    assert 'ExecutionStarted' in output
    assert 'ExecutionSucceeded' in output
    # Terminal event reached on the first fetch, so it must not poll again.
    assert len(client.history_calls) == 1


def test_tail_log_groups_interleaves_sources_by_timestamp():
    # Group A's event is newer than group B's; the merged stream must render B before A, and
    # each line must carry its source prefix.
    client = FakeLogsClient(
        streams=[],
        filter_pages=[
            {'events': [{'eventId': 'a1', 'timestamp': 2000, 'message': 'alpha-msg'}]},
            {'events': [{'eventId': 'b1', 'timestamp': 1000, 'message': 'bravo-msg'}]},
        ],
    )
    sources = [('[cyan]svc-a[/cyan] ', '/aws/lambda/a'), ('[magenta]svc-b[/magenta] ', '/aws/lambda/b')]
    with console.capture() as capture:
        tail_log_groups(client, sources, follow=False)
    output = capture.get()

    assert 'svc-a' in output
    assert 'svc-b' in output
    assert output.index('bravo-msg') < output.index('alpha-msg')
