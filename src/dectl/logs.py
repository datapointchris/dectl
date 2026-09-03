import json
import operator
import re
import time

from botocore.exceptions import ClientError
from rich.markup import escape
from rich.padding import Padding
from rich.syntax import Syntax

from dectl.output import console
from dectl.output import info

GLUE_OUTPUT_LOG_GROUP = '/aws-glue/python-jobs/output'
GLUE_ERROR_LOG_GROUP = '/aws-glue/python-jobs/error'

POLL_SECONDS = 2

# CloudWatch ingestion lags the process that wrote the line, so a run's last few events land
# after the API already reports it finished. Keep polling for a few intervals past the terminal
# state rather than cutting the tail off mid-traceback.
DRAIN_PASSES = 3

TIMESTAMP_KEYS = ('timestamp', 'asctime', 'time')
LEVEL_KEYS = ('level', 'levelname', 'severity')
MESSAGE_KEYS = ('message', 'msg', 'event')
TRACEBACK_KEYS = ('exc_info', 'exception', 'stack_trace', 'stacktrace', 'traceback')

HEADER_KEYS = frozenset(TIMESTAMP_KEYS + LEVEL_KEYS + MESSAGE_KEYS)

# The durable SDK's two opaque operation ids. Neither is a value anyone reads: they identify an
# operation to the service, and the operation's *name* is what a person recognizes, which
# `operation_tag` lifts into the header instead. Deliberately not `requestId` — one execution spans
# many invocations, so that one varies across a scoped tail and says which invocation spoke — and
# deliberately not `logger`, which is ordinary structured-logging output no durable function owns.
DURABLE_OPERATION_ID_KEYS = frozenset({'parentId', 'operationId'})

# Constant only once a tail is scoped to one execution, so `tail_lambda_logs` adds it when its
# filter pattern says the tail is scoped, rather than any caller deciding a second time.
EXECUTION_ARN_KEY = 'executionArn'

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
    a library writing direct) and worth marking; labeling every ordinary line
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


def operation_tag(data: dict) -> tuple[str, frozenset[str]]:
    """The operation a durable record came from, and the keys folded into it.

    Returns the tag and the keys it rendered, never a key it merely read. Claiming one it did not
    show would suppress a field nothing can bring back, since the claim is consulted before
    hide_keys and no flag reaches past it.

    So a first attempt, a non-integer attempt, and an attempt with no operation to belong to all
    fall through to the ordinary field rendering. Only a retry is folded, because only a retry
    changes how the line reads."""
    name = data.get('operationName')
    if not name:
        return '', frozenset()
    attempt = data.get('attempt')
    if isinstance(attempt, int) and attempt > 1:
        return f'{name}.{attempt}', frozenset({'operationName', 'attempt'})
    return str(name), frozenset({'operationName'})


def render_event(message: str, prefix: str, hide_keys: frozenset[str]) -> None:
    """Print a CloudWatch event, restructuring it when the whole message is a
    JSON object. Structured logs (durable functions, python-json-logger) arrive
    as one dense line with any traceback collapsed into a single \\n-escaped
    field; this lifts the common fields into a header and re-expands tracebacks
    into readable, syntax-highlighted frames. Anything else prints verbatim.

    hide_keys drops fields that repeat unchanged on every record; pass an empty set to see the
    whole record. It has no default because the answer differs per caller and a wrong one is
    invisible — a suppressed field leaves a record that still reads as complete."""
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
    tag, tagged_keys = operation_tag(data)
    level_color = LEVEL_COLORS.get(level.upper(), 'white')

    header_parts = []
    if timestamp:
        header_parts.append(f'[cyan]{escape(timestamp)}[/cyan]')
    if level:
        header_parts.append(f'[bold {level_color}]{escape(level)}[/bold {level_color}]')
    if tag:
        header_parts.append(f'[magenta]{escape(tag)}[/magenta]')
    if body:
        header_parts.append(escape(body))
    console.print(f'{prefix}{" ".join(header_parts)}' if header_parts else f'{prefix}{escape(text)}')

    # Hang the expanded fields under the header rather than at column 0, so a prefixed
    # record reads as one block instead of a labeled line followed by orphans.
    indent_width = visible_width(prefix)
    indent = ' ' * indent_width

    for key, value in data.items():
        if key in HEADER_KEYS or key in tagged_keys or key in hide_keys:
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


class LogGroupCursor:
    """A resumable position in one CloudWatch log group, polled with filter_log_events.

    filter_log_events spans every stream in the group, which is what makes it the right primitive
    for all three tailers here: Lambda spreads invocations across per-environment streams, Glue
    creates its error stream only when something first writes to stderr, and monitor merges whole
    groups. Following streams individually means missing every stream created after the tail
    started.

    startTime is inclusive, so the newest events come back again on the next poll. Advancing past
    them would drop any sibling sharing that millisecond, so the boundary is instead deduped by
    eventId.

    **start_time is not optional in practice.** filter_log_events scans the group forward from
    startTime, defaulting to the beginning of its retention, and returns empty pages with a
    nextToken while it scans. On a group shared by every job in the account — which both Glue
    groups are — an unbounded scan pages for minutes before the first event surfaces. Always pass
    a lower bound: the run started, the execution started, now."""

    def __init__(self, log_group: str, start_time: int | None = None, **filters) -> None:
        self.log_group = log_group
        self.start_time = start_time
        self.filters = {key: value for key, value in filters.items() if value}
        self.seen_at_boundary: set[str] = set()
        # A group nothing has ever written to does not exist; that is an ordinary state for a
        # Glue error group, not an error, so callers report it rather than failing, and polling
        # continues in case the group is created mid-run.
        self.missing = False

    def poll(self, logs_client) -> list[dict]:
        fetched: list[dict] = []
        next_token = None
        while True:
            kwargs: dict = {'logGroupName': self.log_group} | self.filters
            if self.start_time is not None:
                kwargs['startTime'] = self.start_time
            if next_token:
                kwargs['nextToken'] = next_token
            try:
                page = logs_client.filter_log_events(**kwargs)
            except ClientError as exc:
                if exc.response.get('Error', {}).get('Code') != 'ResourceNotFoundException':
                    raise
                self.missing = True
                return []
            fetched.extend(page.get('events', []))
            next_token = page.get('nextToken')
            if not next_token:
                break

        self.missing = False
        new_events = [event for event in fetched if event['eventId'] not in self.seen_at_boundary]
        if new_events:
            newest = max(event['timestamp'] for event in new_events)
            self.start_time = newest
            self.seen_at_boundary = {event['eventId'] for event in fetched if event['timestamp'] == newest}
        return new_events


def render_merged(batch: list[tuple[int, str, str]]) -> None:
    """Render events pooled from several cursors in timestamp order, each with its source prefix.

    Timestamp order is not program order across groups, and cannot be made so: a CloudWatch
    timestamp records when a line was shipped, and Glue's stdout and stderr go through
    independent agents. A job that prints, raises, then prints again routinely renders both
    stdout lines before the traceback. The err prefix is what makes that readable."""
    # Glue and monitor records carry none of the durable SDK's fields, and monitor interleaves
    # several sources, so nothing here is repetition worth hiding.
    for _, prefix, message in sorted(batch, key=operator.itemgetter(0)):
        render_event(message, prefix=prefix, hide_keys=frozenset())


def tail_log_groups(logs_client, sources: list[tuple[str, str]], follow: bool = True) -> None:
    """Tail several CloudWatch log groups into one time-ordered stream.

    sources is a list of (prefix, log_group). Each poll fetches new events from every group,
    merges them, and renders in timestamp order so the cross-source causal sequence (lambda A
    fires -> state machine transitions -> lambda B fires) reads top to bottom."""
    now = int(time.time() * 1000)
    cursors = [(prefix, LogGroupCursor(group, start_time=now)) for prefix, group in sources]

    while True:
        batch = []
        for prefix, cursor in cursors:
            for event in cursor.poll(logs_client):
                batch.append((event['timestamp'], prefix, event['message']))
        render_merged(batch)

        if not follow:
            break
        if not batch:
            time.sleep(POLL_SECONDS)


def tail_glue_run(logs_client, run_id: str, started_at: int, follow: bool = True, run_finished=None) -> None:
    """Tail a Glue run's output and error logs, printing each event as soon as it is readable.

    Both Glue log groups are shared by every job in the account, so a run is isolated two ways
    that are each load-bearing:

    * `logStreamNamePrefix=<run id>` picks out this run's streams without naming them, so nothing
      waits for a stream to be created. Polling describe_log_streams instead costs a job that
      never writes to stderr its whole timeout in silence, because no error stream exists for it
      to wait on; and it pins the stream list, so a traceback landing in an error stream created
      later in the run is never shown at all.
    * `started_at` bounds the scan. Filtering by prefix alone is *not* enough: filter_log_events
      scans forward from startTime, and left unset that is the start of the group's retention.
      Against a group holding every Glue run in the account it pages through months of other
      jobs' logs — several minutes before the first line of yours appears, all at once. This is
      the run's own start time, so the window is the length of the run.

    run_finished is an optional predicate polled alongside the logs; when it reports the run over,
    a few more passes drain whatever CloudWatch was still ingesting and the tail returns."""
    info(f'tailing logs for run {run_id}')
    cursors = [
        (group, stream_prefix(group), LogGroupCursor(group, start_time=started_at, logStreamNamePrefix=run_id))
        for group in (GLUE_OUTPUT_LOG_GROUP, GLUE_ERROR_LOG_GROUP)
    ]

    printed_anything = False
    drained_passes = 0
    while True:
        batch = []
        for _, prefix, cursor in cursors:
            for event in cursor.poll(logs_client):
                batch.append((event['timestamp'], prefix, event['message']))
        render_merged(batch)
        printed_anything = printed_anything or bool(batch)

        if not follow:
            break
        if run_finished is not None and run_finished():
            drained_passes += 1
            if drained_passes >= DRAIN_PASSES:
                break
        time.sleep(POLL_SECONDS)

    if printed_anything:
        return
    console.print('[yellow]no log events for this run[/yellow]')
    # Distinguish "the job printed nothing" from "there is nowhere for it to print": a missing
    # group means no job in this account has ever written there, which usually means the run
    # failed before the script started, or the job is Spark and logs to /aws-glue/jobs/*.
    for group, _, cursor in cursors:
        if cursor.missing:
            info(f'log group {group} does not exist')


def tail_lambda_logs(
    logs_client,
    function_name: str,
    hide_keys: frozenset[str],
    fold_scope_fields: bool,
    follow: bool = True,
    filter_pattern: str = '',
    start_time: int | None = None,
    end_time: int | None = None,
) -> None:
    """Tail a Lambda function's log group.

    filter_pattern / start_time / end_time narrow the tail to one durable execution's records;
    left at their defaults the whole group is followed from the current invocation onwards.

    Two conditions decide whether the execution ARN is worth a line, and both are needed. A
    non-empty filter_pattern says the tail is scoped to one execution, which is what makes the ARN
    constant — that fact lives here, so the ARN is added here rather than recomputed per caller.
    fold_scope_fields says the caller wants folding at all, and it is a parameter rather than an
    empty hide_keys because emptiness already means "this caller has nothing to suppress" for
    glue and the non-durable logs."""
    if filter_pattern and fold_scope_fields:
        hide_keys = hide_keys | {EXECUTION_ARN_KEY}
    log_group = f'/aws/lambda/{function_name}'
    info(f'tailing {log_group}')

    # Lambda writes each execution environment to its own log stream, and an invocation that
    # cold-starts (after the previous warm environment is reaped, ~5-15 min idle) lands in a
    # brand-new stream. Following a single stream would silently miss those later runs, so poll
    # the whole log group by time with filter_log_events, which spans every stream.
    if start_time is None:
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

    cursor = LogGroupCursor(log_group, start_time=start_time, filterPattern=filter_pattern, endTime=end_time)
    while True:
        new_events = cursor.poll(logs_client)
        for event in new_events:
            render_event(event['message'], prefix='', hide_keys=hide_keys)

        if not new_events:
            if not follow:
                break
            time.sleep(POLL_SECONDS)
