import contextlib
import operator
import os

import pytest
from botocore.exceptions import ClientError
from typer.testing import CliRunner

# Every rendered assertion in this suite is otherwise an assertion about the window the suite was
# run in. A rich console wraps to the terminal it finds, so `'is not a name S3 will accept' in
# result.stderr` passes at 80 columns and fails at 40 on output correct at both, and a table
# folds a UUID across rows or truncates it with an ellipsis depending on the same number.
# Measured at 40 columns: thirteen tests across six files, none of them about width.
#
# Set before `dectl.output` builds its two consoles, which is why it is a module-level statement
# rather than a fixture. Wide enough that every table under test renders its columns unfolded,
# which is what makes an assertion about a rendered value an assertion about the value.
#
# A test whose subject *is* the width sets its own, through `at_width`. Pinning wide would make
# those pass on a table that folds and on one that truncates alike.
os.environ['COLUMNS'] = '160'


class RefusalRunner(CliRunner):
    """A `CliRunner` that reports a refusal and re-raises a crash.

    Click reports exit code 1 for both an unhandled exception and a `typer.Exit(1)`, so
    `exit_code == 1` beside `stdout == ''` is satisfied by any crash before the first print —
    the assertion reads as "the tool refused" and means "the tool did not get that far". That is
    not hypothetical: inserting `raise IndexError` as the first line of `build_job_update` left
    three refusal tests green, and the crash it stood in for was a live bug.

    Turning the catch off makes the distinction structural rather than something each test has
    to opt into. `typer.Exit` reaches the runner as `SystemExit`, which click handles whatever
    this is set to, so every genuine refusal still arrives as a result with an exit code. Every
    other exception propagates and fails its test by name.

    A test that means to drive a crash passes `catch_exceptions=True` at the call and says why."""

    def invoke(self, *args, **kwargs):
        kwargs.setdefault('catch_exceptions', False)
        return super().invoke(*args, **kwargs)


class FakeCloudWatchLogs:
    """CloudWatch Logs stand-in that enforces the constraints the real service does.

    Every log-tailing bug that has shipped from this repo was invisible to a fake that was more
    permissive than CloudWatch. A fake which returns whatever it was handed regardless of
    startTime, logStreamNamePrefix or filterPattern makes a tailer that forgets to narrow its
    query look identical to one that does — and the difference between those two is minutes of
    silence against a log group shared by every job in the account.

    So this applies each filter for real, and refuses the one call shape that cannot be judged
    from its results: a filter_log_events with no startTime, which scans from the start of the
    group's retention. Events carry logStreamName because the prefix filter is what isolates a
    Glue run, and the timestamps are meaningful because startTime is inclusive — the boundary
    event legitimately comes back on the next poll, and deduping it is the tailer's job.
    """

    def __init__(self, groups: dict[str, list[dict]] | None = None, missing_groups: tuple[str, ...] = ()) -> None:
        self.groups = groups or {}
        self.missing_groups = missing_groups
        self.filter_calls: list[dict] = []
        self.describe_calls: list[dict] = []

    def describe_log_streams(self, **kwargs) -> dict:
        self.describe_calls.append(kwargs)
        group = kwargs['logGroupName']
        self.reject_missing(group)
        names = {event['logStreamName'] for event in self.groups.get(group, [])}
        streams = [
            {
                'logStreamName': name,
                'firstEventTimestamp': min(e['timestamp'] for e in self.groups[group] if e['logStreamName'] == name),
            }
            for name in sorted(names)
        ]
        return {'logStreams': streams}

    def filter_log_events(self, **kwargs) -> dict:
        self.filter_calls.append(kwargs)
        group = kwargs['logGroupName']
        self.reject_missing(group)
        if 'startTime' not in kwargs:
            raise AssertionError(
                f'filter_log_events on {group} with no startTime scans the group from the start of its '
                'retention — against a shared group that is minutes of paging before the first event. '
                'Every tailer must pass a lower bound.'
            )

        events = list(self.groups.get(group, []))
        events = [event for event in events if event['timestamp'] >= kwargs['startTime']]
        if 'endTime' in kwargs:
            events = [event for event in events if event['timestamp'] <= kwargs['endTime']]
        if 'logStreamNamePrefix' in kwargs:
            events = [event for event in events if event['logStreamName'].startswith(kwargs['logStreamNamePrefix'])]
        if 'filterPattern' in kwargs:
            # dectl only builds quoted-substring patterns; matching that shape is enough here.
            needle = kwargs['filterPattern'].strip('"')
            events = [event for event in events if needle in event['message']]
        return {'events': sorted(events, key=operator.itemgetter('timestamp'))}

    def reject_missing(self, group: str) -> None:
        if group in self.missing_groups:
            raise ClientError(
                {'Error': {'Code': 'ResourceNotFoundException', 'Message': 'The specified log group does not exist'}},
                'FilterLogEvents',
            )


@contextlib.contextmanager
def at_width(console, columns: int):
    """Render into `console` at a fixed width, for a test whose subject is the width.

    The suite pins a wide console so a rendered value is not a fact about the runner's window.
    That is the right default and the wrong one for a guard on folding: a column wide enough to
    hold its value renders identically whether it folds, truncates or does neither, so such a
    test passes on the behaviour it was written to refuse. Those set the width they mean."""
    original = console.width
    console.width = columns
    try:
        yield
    finally:
        console.width = original


def log_event(event_id: str, timestamp: int, message: str, stream: str = 'stream-a') -> dict:
    return {'eventId': event_id, 'timestamp': timestamp, 'message': message, 'logStreamName': stream}


def pytest_addoption(parser):
    parser.addoption(
        '--run-integration',
        action='store_true',
        default=False,
        help='Run live AWS integration tests. These create and delete real AWS resources and require credentials.',
    )


def pytest_collection_modifyitems(config, items):
    if config.getoption('--run-integration'):
        return
    skip_integration = pytest.mark.skip(reason='live AWS test; pass --run-integration to run')
    for item in items:
        if 'integration' in item.keywords:
            item.add_marker(skip_integration)
