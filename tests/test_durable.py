from datetime import datetime
from datetime import timedelta

import pytest
import typer

from dectl.config import LambdaConfig
from dectl.durable import epoch_millis
from dectl.durable import format_duration
from dectl.durable import qualifier_for
from dectl.durable import render_durable_event
from dectl.durable import render_execution_header
from dectl.durable import resolve_execution
from dectl.durable import tail_durable_history
from dectl.output import console

STARTED = datetime(2026, 7, 30, 12)


class FakeLambdaClient:
    """Lambda stand-in for the durable execution APIs."""

    def __init__(self, executions: list[dict] | None = None, history_pages: list[dict] | None = None) -> None:
        self.executions = executions or []
        self.history_pages = history_pages or []
        self.list_calls: list[dict] = []
        self.get_calls: list[dict] = []
        self.history_calls: list[dict] = []

    def list_durable_executions_by_function(self, **kwargs) -> dict:
        self.list_calls.append(kwargs)
        matches = self.executions
        if kwargs.get('DurableExecutionName'):
            matches = [e for e in matches if e['DurableExecutionName'] == kwargs['DurableExecutionName']]
        return {'DurableExecutions': matches[: kwargs.get('MaxItems', 100)]}

    def get_durable_execution(self, **kwargs) -> dict:
        self.get_calls.append(kwargs)
        for execution in self.executions:
            if execution['DurableExecutionArn'] == kwargs['DurableExecutionArn']:
                return execution
        return {'DurableExecutionArn': kwargs['DurableExecutionArn'], 'Status': 'SUCCEEDED'}

    def get_durable_execution_history(self, **kwargs) -> dict:
        self.history_calls.append(kwargs)
        if self.history_pages:
            return self.history_pages.pop(0)
        return {'Events': []}


def execution(name: str, status: str = 'SUCCEEDED', ended: datetime | None = None) -> dict:
    return {
        'DurableExecutionArn': f'arn:aws:lambda:us-east-2:1:function:fn:live/durable-execution/{name}/x',
        'DurableExecutionName': name,
        'Status': status,
        'StartTimestamp': STARTED,
        'EndTimestamp': ended,
    }


def capture_durable_event(event: dict) -> str:
    with console.capture() as capture:
        render_durable_event(event)
    return capture.get()


def test_qualifier_prefers_the_live_alias():
    fn = LambdaConfig(name='fn', source_dir='code', live_alias='live', durable=True)
    assert qualifier_for(fn) == 'live'


def test_qualifier_falls_back_to_latest():
    # Durable invocations are always qualified; without a live alias, $LATEST is the qualifier.
    fn = LambdaConfig(name='fn', source_dir='code', durable=True)
    assert qualifier_for(fn) == '$LATEST'


def test_resolve_execution_defaults_to_the_most_recent():
    client = FakeLambdaClient([execution('order-9'), execution('order-8')])

    found = resolve_execution(client, 'fn', 'live')

    assert found['DurableExecutionName'] == 'order-9'
    assert client.list_calls[0]['MaxItems'] == 1
    assert client.list_calls[0]['Qualifier'] == 'live'


def test_resolve_execution_looks_up_a_bare_name():
    client = FakeLambdaClient([execution('order-9'), execution('order-8')])

    found = resolve_execution(client, 'fn', 'live', 'order-8')

    assert found['DurableExecutionName'] == 'order-8'
    assert client.list_calls[0]['DurableExecutionName'] == 'order-8'


def test_resolve_execution_uses_an_arn_directly():
    client = FakeLambdaClient([execution('order-9')])
    arn = execution('order-9')['DurableExecutionArn']

    found = resolve_execution(client, 'fn', 'live', arn)

    assert found['DurableExecutionName'] == 'order-9'
    # An ARN identifies the execution outright, so there is nothing to search for.
    assert client.list_calls == []


def test_resolve_execution_exits_when_nothing_matches():
    client = FakeLambdaClient([])

    with pytest.raises(typer.Exit):
        resolve_execution(client, 'fn', 'live', 'no-such-run')


def test_format_duration_covers_a_suspended_workflow():
    # Elapsed time spans durable waits, so it is routinely hours rather than seconds.
    assert format_duration(STARTED, STARTED + timedelta(seconds=4.2)) == '4.2s'
    assert format_duration(STARTED, STARTED + timedelta(minutes=3, seconds=7)) == '3m07s'
    assert format_duration(STARTED, STARTED + timedelta(hours=26, minutes=5)) == '26h05m'
    assert format_duration(STARTED, None) == ''


def test_epoch_millis_converts_a_boto_timestamp():
    assert epoch_millis(STARTED) == int(STARTED.timestamp() * 1000)
    assert epoch_millis(None) is None


def test_render_durable_event_names_the_step_and_its_result():
    event = {
        'EventType': 'StepSucceeded',
        'EventTimestamp': STARTED,
        'Name': 'fetch-orders',
        'StepSucceededDetails': {'Result': {'Payload': '{"count": 3}'}},
    }
    output = capture_durable_event(event)

    assert 'StepSucceeded' in output
    assert 'fetch-orders' in output
    assert '{"count": 3}' in output


def test_render_durable_event_expands_a_step_failure():
    event = {
        'EventType': 'StepFailed',
        'EventTimestamp': STARTED,
        'Name': 'charge-card',
        'StepFailedDetails': {
            'Error': {
                'Payload': {
                    'ErrorType': 'ValueError',
                    'ErrorMessage': 'card declined',
                    'StackTrace': ['  File "handler.py", line 12', '    raise ValueError'],
                }
            },
            'RetryDetails': {'CurrentAttempt': 3},
        },
    }
    output = capture_durable_event(event)

    assert 'ValueError' in output
    assert 'card declined' in output
    assert 'handler.py' in output
    assert 'attempt' in output


def test_render_durable_event_shows_a_wait_duration():
    event = {'EventType': 'WaitStarted', 'EventTimestamp': STARTED, 'WaitStartedDetails': {'Duration': 3600}}
    assert '3600s' in capture_durable_event(event)


def test_render_execution_header_surfaces_the_failure_reason():
    # GetDurableExecution returns Error unwrapped, unlike the history events' Payload envelope.
    failed = execution('order-9', status='FAILED', ended=STARTED + timedelta(seconds=5))
    failed['Error'] = {'ErrorType': 'RuntimeError', 'ErrorMessage': 'step exhausted retries'}

    with console.capture() as capture:
        render_execution_header(failed)
    output = capture.get()

    assert 'FAILED' in output
    assert 'step exhausted retries' in output


def test_tail_durable_history_stops_at_a_terminal_event():
    events = [
        {'EventId': 1, 'EventType': 'ExecutionStarted', 'EventTimestamp': STARTED},
        {'EventId': 2, 'EventType': 'StepSucceeded', 'EventTimestamp': STARTED, 'Name': 'work'},
        {'EventId': 3, 'EventType': 'ExecutionSucceeded', 'EventTimestamp': STARTED},
    ]
    client = FakeLambdaClient(history_pages=[{'Events': events}])

    with console.capture() as capture:
        tail_durable_history(client, 'arn:exec', follow=True)
    output = capture.get()

    assert 'ExecutionSucceeded' in output
    assert len(client.history_calls) == 1


def test_tail_durable_history_follows_pagination():
    client = FakeLambdaClient(
        history_pages=[
            {'Events': [{'EventId': 1, 'EventType': 'ExecutionStarted', 'EventTimestamp': STARTED}], 'NextMarker': 'more'},
            {'Events': [{'EventId': 2, 'EventType': 'ExecutionSucceeded', 'EventTimestamp': STARTED}]},
        ]
    )

    with console.capture() as capture:
        tail_durable_history(client, 'arn:exec', follow=False)
    output = capture.get()

    assert 'ExecutionStarted' in output
    assert 'ExecutionSucceeded' in output
    assert client.history_calls[1]['Marker'] == 'more'
