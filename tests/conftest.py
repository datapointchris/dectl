import operator

import pytest
from botocore.exceptions import ClientError


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
