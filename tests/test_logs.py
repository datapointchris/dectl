import datetime as dt
import json
import time

import pytest

from dectl.logs import DURABLE_OPERATION_ID_KEYS
from dectl.logs import GLUE_ERROR_LOG_GROUP
from dectl.logs import GLUE_OUTPUT_LOG_GROUP
from dectl.logs import operation_tag
from dectl.logs import render_event
from dectl.logs import render_history_event
from dectl.logs import render_merged
from dectl.logs import tail_execution_history
from dectl.logs import tail_glue_run
from dectl.logs import tail_lambda_logs
from dectl.logs import tail_log_groups
from dectl.output import console
from tests.conftest import FakeCloudWatchLogs
from tests.conftest import log_event


def capture_event(message: str, prefix: str = '', hide_keys: frozenset[str] = frozenset()) -> str:
    with console.capture() as capture:
        render_event(message, prefix=prefix, hide_keys=hide_keys)
    return capture.get()


DURABLE_RECORD = {
    'timestamp': '2026-07-08T12:00:00',
    'level': 'INFO',
    'message': 'avscan/b-1: 3/10 files',
    'logger': 'root',
    'requestId': '8a7c1f2e-9b3d-4c5a-8e7f-1a2b3c4d5e6f',
    'executionArn': 'arn:aws:lambda:us-east-2:1:function:fn:7/durable-execution/090c4189/9f7d84c9',
    'operationId': '1ced8f5be2db23a6513eba4d819c7380',
    'operationName': 'wait_for_files',
    'attempt': 3,
}


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


def test_render_event_expands_collapsed_traceback():
    traceback = 'Traceback (most recent call last):\n  File "job.py", line 3\n    raise ValueError("x")\nValueError: x'
    message = json.dumps({'level': 'ERROR', 'message': 'failed', 'exc_info': traceback})
    output = capture_event(message)
    assert 'Traceback (most recent call last):' in output
    assert 'ValueError: x' in output
    # the \n-escaped field is re-expanded onto multiple lines, not one
    assert output.count('\n') >= 4


LAMBDA_GROUP = '/aws/lambda/my-func'


def test_lambda_tail_picks_up_events_across_streams():
    # The bug: a later invocation cold-starts into a new log stream. filter_log_events spans
    # the whole group, so events from both streams surface in a single tail session.
    client = FakeCloudWatchLogs(
        {
            LAMBDA_GROUP: [
                log_event('a', 2000, 'warm run', stream='stream-A'),
                log_event('b', 3000, 'cold-start run', stream='stream-B'),
            ]
        }
    )
    with console.capture() as capture:
        tail_lambda_logs(client, 'my-func', hide_keys=frozenset(), fold_scope_fields=False, follow=False)
    output = capture.get()

    assert 'warm run' in output
    assert 'cold-start run' in output


def test_lambda_tail_does_not_reprint_boundary_events():
    # startTime is inclusive, so the fake genuinely re-returns the newest event on the next poll,
    # exactly as CloudWatch does. It must not print twice. A raising sleep breaks the follow loop.
    class StopTail(Exception):
        pass

    client = FakeCloudWatchLogs({LAMBDA_GROUP: [log_event('a', 2000, 'event-one'), log_event('b', 2001, 'event-two')]})

    def stop_sleep(_seconds):
        raise StopTail

    with pytest.MonkeyPatch.context() as monkeypatch, console.capture() as capture:
        monkeypatch.setattr('dectl.logs.time.sleep', stop_sleep)
        with pytest.raises(StopTail):
            tail_lambda_logs(client, 'my-func', hide_keys=frozenset(), fold_scope_fields=False, follow=True)
    output = capture.get()

    assert output.count('event-one') == 1
    assert output.count('event-two') == 1


def test_render_history_event_surfaces_error_and_cause():
    event = {
        'type': 'ExecutionFailed',
        'timestamp': dt.datetime(2026, 7, 13, 12),
        'executionFailedEventDetails': {'error': 'States.TaskFailed', 'cause': 'lambda blew up'},
    }
    output = capture_history_event(event)
    assert 'ExecutionFailed' in output
    assert 'States.TaskFailed' in output
    assert 'lambda blew up' in output


def test_tail_execution_history_stops_at_terminal_event():
    timestamp = dt.datetime(2026, 7, 13, 12)
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


RUN_START = 1_700_000_000_000


def glue_event(event_id: str, timestamp: int, message: str) -> dict:
    # Glue names a Python Shell run's stream after the run id, in both shared groups; that is
    # what the prefix filter isolates, and the integration test asserts it against real Glue.
    return log_event(event_id, timestamp, message, stream='jr_abc123')


def test_glue_tail_filters_both_groups_by_run_id_without_waiting_for_streams():
    # Nothing may block on a stream existing. Polling describe_log_streams for the output
    # stream and then, sequentially, for the error stream costs up to two minutes of silence
    # before the first line whenever a job never writes to stderr.
    client = FakeCloudWatchLogs({GLUE_OUTPUT_LOG_GROUP: [glue_event('a', RUN_START + 10, 'first line')]})

    with console.capture() as capture:
        tail_glue_run(client, 'jr_abc123', RUN_START, follow=False)
    output = capture.get()

    assert 'first line' in output
    assert client.describe_calls == [], 'nothing may block on a stream existing'
    groups = {call['logGroupName'] for call in client.filter_calls}
    assert groups == {GLUE_OUTPUT_LOG_GROUP, GLUE_ERROR_LOG_GROUP}
    assert all(call['logStreamNamePrefix'] == 'jr_abc123' for call in client.filter_calls)


def test_glue_tail_says_when_a_log_group_does_not_exist():
    # "No events" and "nowhere for events to go" are different diagnoses; the second usually means
    # the run died before the script ran, or it is a Spark job logging to /aws-glue/jobs/*.
    client = FakeCloudWatchLogs({}, missing_groups=(GLUE_OUTPUT_LOG_GROUP, GLUE_ERROR_LOG_GROUP))

    with console.capture() as capture:
        tail_glue_run(client, 'jr_abc123', RUN_START, follow=False)
    output = capture.get()

    assert 'no log events' in output
    assert GLUE_OUTPUT_LOG_GROUP in output


def test_glue_tail_prints_output_when_the_error_group_does_not_exist():
    # A Glue error group with nothing ever written to it is 404, not a failure — the run's stdout
    # must still stream.
    client = FakeCloudWatchLogs(
        {GLUE_OUTPUT_LOG_GROUP: [glue_event('a', RUN_START + 10, 'still working')]},
        missing_groups=(GLUE_ERROR_LOG_GROUP,),
    )

    with console.capture() as capture:
        tail_glue_run(client, 'jr_abc123', RUN_START, follow=False)

    assert 'still working' in capture.get()


def test_glue_tail_merges_the_two_groups_in_timestamp_order():
    client = FakeCloudWatchLogs(
        {
            GLUE_OUTPUT_LOG_GROUP: [glue_event('a', RUN_START + 2000, 'after-the-error')],
            GLUE_ERROR_LOG_GROUP: [glue_event('b', RUN_START + 1000, 'the-traceback')],
        }
    )

    with console.capture() as capture:
        tail_glue_run(client, 'jr_abc123', RUN_START, follow=False)
    output = capture.get()

    assert output.index('the-traceback') < output.index('after-the-error')
    assert 'err the-traceback' in output


def test_glue_tail_stops_once_the_run_reaches_a_terminal_state(monkeypatch):
    # `run --follow` returns on its own: the tail stops at a terminal state rather than
    # looping until the reader interrupts it.
    monkeypatch.setattr('dectl.logs.time.sleep', lambda _seconds: None)
    client = FakeCloudWatchLogs({GLUE_OUTPUT_LOG_GROUP: [glue_event('a', RUN_START + 10, 'done')]})

    with console.capture():
        tail_glue_run(client, 'jr_abc123', RUN_START, follow=True, run_finished=lambda: True)

    # Two groups per pass, drained for a fixed number of passes rather than indefinitely.
    assert len(client.filter_calls) == 2 * 3


def test_lambda_tail_scopes_to_one_durable_execution():
    # The durable case: same log group, narrowed to the records the SDK logger stamped with this
    # execution's ARN and to the window the execution ran in.
    client = FakeCloudWatchLogs(
        {
            '/aws/lambda/my-fn': [
                log_event('a', 500, 'step ran arn:exec'),
                log_event('b', 500, 'another execution arn:other'),
                log_event('c', 5000, 'after the window arn:exec'),
            ]
        }
    )

    with console.capture() as capture:
        tail_lambda_logs(
            client,
            'my-fn',
            hide_keys=frozenset(),
            fold_scope_fields=False,
            follow=False,
            filter_pattern='"arn:exec"',
            start_time=100,
            end_time=900,
        )
    output = capture.get()

    assert 'step ran' in output
    # The fake applies the filters, so these prove the scoping rather than just the call shape.
    assert 'another execution' not in output
    assert 'after the window' not in output
    assert client.filter_calls[0]['filterPattern'] == '"arn:exec"'
    assert client.filter_calls[0]['startTime'] == 100
    assert client.filter_calls[0]['endTime'] == 900
    # An explicit window means no need to guess a start from the newest stream.
    assert client.describe_calls == []


def test_tail_log_groups_interleaves_sources_by_timestamp():
    # Group A's event is newer than group B's; the merged stream must render B before A, and
    # each line must carry its source prefix.
    now_ms = int(time.time() * 1000)
    client = FakeCloudWatchLogs(
        {
            '/aws/lambda/a': [log_event('a1', now_ms + 2000, 'alpha-msg')],
            '/aws/lambda/b': [log_event('b1', now_ms + 1000, 'bravo-msg')],
        }
    )
    sources = [('[cyan]svc-a[/cyan] ', '/aws/lambda/a'), ('[magenta]svc-b[/magenta] ', '/aws/lambda/b')]
    with console.capture() as capture:
        tail_log_groups(client, sources, follow=False)
    output = capture.get()

    assert 'svc-a' in output
    assert 'svc-b' in output
    assert output.index('bravo-msg') < output.index('alpha-msg')


def test_render_event_folds_the_opaque_durable_ids_out_of_a_scoped_record():
    # The two operation ids identify an operation to the service and are unreadable; the execution
    # ARN is the filter this tail already applied. requestId stays, because one execution spans
    # many invocations and it is what says which one spoke.
    output = capture_event(json.dumps(DURABLE_RECORD), hide_keys=DURABLE_OPERATION_ID_KEYS | {'executionArn'})

    assert 'avscan/b-1: 3/10 files' in output
    assert 'wait_for_files' in output
    assert 'executionArn' not in output
    assert 'operationId' not in output
    assert 'requestId' in output


def test_operation_tag_marks_a_retry_and_leaves_the_first_attempt_bare():
    assert operation_tag({'operationName': 'stage', 'attempt': 1})[0] == 'stage'
    assert operation_tag({'operationName': 'stage', 'attempt': 4})[0] == 'stage.4'
    assert operation_tag({'attempt': 4})[0] == ''


def test_operation_tag_only_claims_the_keys_it_showed():
    # What the tag consumed is what render_event suppresses, and the claim is read before
    # hide_keys, so a key claimed without being shown is one no flag can bring back.
    assert operation_tag({'operationName': 'stage', 'attempt': 4})[1] == frozenset({'operationName', 'attempt'})
    assert operation_tag({'operationName': 'stage', 'attempt': 1})[1] == frozenset({'operationName'})
    assert operation_tag({'operationName': 'stage', 'attempt': 'four'})[1] == frozenset({'operationName'})
    assert operation_tag({'attempt': 4})[1] == frozenset()


def test_glue_and_monitor_records_keep_every_field():
    # render_merged is the glue and monitor path. It asks for no suppression, and the invariant the
    # narrow signature enforced was that every field past the header prints. This points at the
    # caller rather than at render_event, because a default is what would break it.
    message = json.dumps({'level': 'INFO', 'message': 'row', 'requestId': 'r-1', 'logger': 'job', 'rows': 42})
    with console.capture() as capture:
        render_merged([(2000, '', message)])
    output = capture.get()

    assert 'requestId' in output
    assert 'logger' in output
    assert 'rows' in output
