import datetime as dt

import pytest
import typer
from botocore.exceptions import ClientError

from dectl.durable import format_duration
from dectl.durable import listing_qualifier
from dectl.durable import render_durable_event
from dectl.durable import render_execution_header
from dectl.durable import resolve_execution
from dectl.durable import sweep_executions
from dectl.durable import tail_durable_history
from dectl.durable import version_of
from dectl.output import console
from dectl.output import stderr_console

STARTED = dt.datetime(2026, 7, 30, 12)


class FakeLambdaClient:
    """Lambda stand-in for the durable execution APIs.

    Executions are keyed by the version in their ARN, matching the service: a list call filters to
    exactly one qualifier, which is what makes an alias unusable there."""

    def __init__(
        self,
        executions: list[dict] | None = None,
        history_pages: list[dict] | None = None,
        aliases: dict[str, str] | None = None,
        versions: list[str] | None = None,
    ) -> None:
        self.executions = executions or []
        self.history_pages = history_pages or []
        self.aliases = aliases or {}
        self.versions = versions or ['$LATEST', '1']
        self.list_calls: list[dict] = []
        self.get_calls: list[dict] = []
        self.history_calls: list[dict] = []

    def get_alias(self, FunctionName, Name) -> dict:
        if Name not in self.aliases:
            raise ClientError({'Error': {'Code': 'ResourceNotFoundException', 'Message': 'no alias'}}, 'GetAlias')
        return {'FunctionVersion': self.aliases[Name]}

    def list_versions_by_function(self, **kwargs) -> dict:
        return {'Versions': [{'Version': version} for version in self.versions]}

    def list_durable_executions_by_function(self, **kwargs) -> dict:
        self.list_calls.append(kwargs)
        qualifier = kwargs.get('Qualifier')
        if qualifier is not None and not (qualifier.startswith('$LATEST') or qualifier.isdigit()):
            raise ClientError(
                {'Error': {'Code': 'InvalidParameterValueException', 'Message': 'cannot filter durable executions by alias'}},
                'ListDurableExecutionsByFunction',
            )
        matches = [e for e in self.executions if version_of(e) == qualifier]
        if kwargs.get('DurableExecutionName'):
            matches = [e for e in matches if e['DurableExecutionName'] == kwargs['DurableExecutionName']]
        # The service narrows on Statuses too. Ignoring it here made a listing's scope
        # unexpressible, which is what hid a resolver reading one listing and consuming another.
        if kwargs.get('Statuses'):
            matches = [e for e in matches if e.get('Status') in kwargs['Statuses']]
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


def execution(name: str, status: str = 'SUCCEEDED', ended: dt.datetime | None = None, version: str = '7', started=STARTED) -> dict:
    return {
        'DurableExecutionArn': f'arn:aws:lambda:us-east-2:1:function:fn:{version}/durable-execution/{name}/x',
        'DurableExecutionName': name,
        'Status': status,
        'StartTimestamp': started,
        'EndTimestamp': ended,
    }


def capture_durable_event(event: dict) -> str:
    with console.capture() as capture:
        render_durable_event(event)
    return capture.get()


def test_listing_qualifier_exits_when_the_alias_does_not_exist():
    client = FakeLambdaClient(aliases={})

    with pytest.raises(typer.Exit):
        listing_qualifier(client, 'fn', 'live')


def test_sweep_merges_versions_newest_execution_first():
    # After a deploy --publish the alias points at a new version, leaving the previous run's
    # executions under the old one; a sweep is what finds them again.
    client = FakeLambdaClient(
        [
            execution('old-run', version='6', started=dt.datetime(2026, 7, 30, 10)),
            execution('new-run', started=dt.datetime(2026, 7, 30, 12)),
        ],
        versions=['$LATEST', '6', '7'],
    )

    found, scanned = sweep_executions(client, 'fn', limit=10)

    assert [e['DurableExecutionName'] for e in found] == ['new-run', 'old-run']
    assert scanned == ['7', '6', '$LATEST']


def test_resolve_execution_defaults_to_the_most_recent():
    client = FakeLambdaClient([execution('order-9'), execution('order-8')], aliases={'live': '7'})

    found = resolve_execution(client, 'fn', '7')

    assert found['DurableExecutionName'] == 'order-9'
    assert client.list_calls[0]['MaxItems'] == 1
    assert client.list_calls[0]['Qualifier'] == '7'


def test_resolve_execution_looks_up_a_bare_name():
    client = FakeLambdaClient([execution('order-9'), execution('order-8')], aliases={'live': '7'})

    found = resolve_execution(client, 'fn', '7', 'order-8')

    assert found['DurableExecutionName'] == 'order-8'
    assert client.list_calls[0]['DurableExecutionName'] == 'order-8'


def test_resolve_execution_falls_back_to_older_versions_for_a_name():
    # The name came from the console; which version ran it is not something you can be expected
    # to know, and after a deploy it is no longer the one the alias points at.
    client = FakeLambdaClient([execution('order-8', version='6')], versions=['$LATEST', '6', '7'])

    found = resolve_execution(client, 'fn', '7', 'order-8')

    assert found['DurableExecutionName'] == 'order-8'


def test_resolve_execution_uses_an_arn_directly():
    client = FakeLambdaClient([execution('order-9')])
    arn = execution('order-9')['DurableExecutionArn']

    found = resolve_execution(client, 'fn', '7', arn)

    assert found['DurableExecutionName'] == 'order-9'
    # An ARN identifies the execution outright, so there is nothing to search for.
    assert client.list_calls == []


def test_resolve_execution_exits_when_nothing_matches():
    client = FakeLambdaClient([])

    with pytest.raises(typer.Exit):
        resolve_execution(client, 'fn', '7', 'no-such-run')


def test_format_duration_covers_a_suspended_workflow():
    # Elapsed time spans durable waits, so it runs from seconds to months. Days are their own
    # unit for that reason: a wait of a quarter reported in hours is a number nobody can read.
    assert format_duration(STARTED, STARTED + dt.timedelta(seconds=4.2)) == '4.2s'
    assert format_duration(STARTED, STARTED + dt.timedelta(minutes=3, seconds=7)) == '3m07s'
    assert format_duration(STARTED, STARTED + dt.timedelta(hours=5, minutes=5)) == '5h05m'
    assert format_duration(STARTED, STARTED + dt.timedelta(hours=26, minutes=5)) == '1d02h'
    assert format_duration(STARTED, STARTED + dt.timedelta(days=91)) == '91d00h'
    assert format_duration(STARTED, None) == ''


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


def test_render_execution_header_surfaces_the_failure_reason():
    # GetDurableExecution returns Error unwrapped, unlike the history events' Payload envelope.
    failed = execution('order-9', status='FAILED', ended=STARTED + dt.timedelta(seconds=5))
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


def test_resolve_execution_takes_a_unique_tail_of_the_name():
    # Lambda names an unnamed execution with a UUID; its tail is the only part anyone retypes.
    client = FakeLambdaClient(
        [execution('090c4189-b18b-4296-9d0c-cfd01dc3a122'), execution('7f2a9c31-4d5e-4a1b-9c8d-2e3f4a5b6c7d')],
        versions=['$LATEST', '7'],
    )

    found = resolve_execution(client, 'fn', '7', 'cfd01dc3a122')

    assert found['DurableExecutionName'] == '090c4189-b18b-4296-9d0c-cfd01dc3a122'


def test_resolve_execution_prefers_a_whole_name_over_a_tail_of_another():
    # 'run-7' is both a name and the tail of 'nightly-run-7'. The exact name wins itself.
    client = FakeLambdaClient([execution('nightly-run-7'), execution('run-7')], versions=['$LATEST', '7'])

    found = resolve_execution(client, 'fn', '7', 'run-7')

    assert found['DurableExecutionName'] == 'run-7'


def test_an_ambiguous_tail_is_a_usage_error_naming_the_candidates():
    # Picking the newest would resolve silently to something the caller did not name.
    client = FakeLambdaClient([execution('batch-alpha-99'), execution('batch-beta-99')], versions=['$LATEST', '7'])

    with pytest.raises(typer.Exit) as raised, stderr_console.capture() as capture:
        resolve_execution(client, 'fn', '7', '99')

    assert raised.value.exit_code == 2
    output = capture.get()
    assert 'batch-alpha-99' in output
    assert 'batch-beta-99' in output
