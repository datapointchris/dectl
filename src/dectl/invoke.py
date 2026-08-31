"""Issuing a Lambda Invoke: how long to wait for one, which client to send it through, and which
failures may safely be sent again.

Kept out of `session.py` for the reason `durable.py` is kept out of `logs.py`: this is the Lambda
API, not the boto3 session. `session.py` builds a session from config and knows nothing about what
is invoked through it."""

import time

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError
from botocore.exceptions import ConnectTimeoutError
from botocore.exceptions import EndpointConnectionError

from dectl.config import LambdaConfig
from dectl.output import warn

# Invoke is the one call dectl makes that AWS cannot take back, so its socket has to outlive the
# function rather than the other way round. The margin sits above the function's own Timeout, where
# Lambda kills the run first, so a socket that does expire means the network and not a slow handler.
INVOKE_TIMEOUT_MARGIN_SECONDS = 30

# Lambda's own ceiling on a function's Timeout, which makes it the answer when the real one cannot
# be read.
LAMBDA_MAX_TIMEOUT_SECONDS = 900

# A synchronous durable invoke waits for the whole execution rather than one invocation, and Lambda
# caps that wait at 15 minutes whatever the function's Timeout says. Equal to the ceiling above by
# coincidence of AWS's limits, not because either follows the other.
DURABLE_SYNC_CAP_SECONDS = 900

# An Event invoke returns as soon as Lambda has the event, so this waits on an acknowledgement
# rather than on any function. Nothing here scales with what the function does.
EVENT_ACK_TIMEOUT_SECONDS = 30

# Opening the connection is not the part that takes minutes, and a connection that never opened has
# invoked nothing.
INVOKE_CONNECT_TIMEOUT_SECONDS = 10

# Lambda refuses the invocation outright under these, so no execution starts and sending it again
# cannot duplicate one. They are the only failures re-issued here, and re-issuing them is what the
# no-retry client would otherwise cost: a function at its concurrency limit is routine, and botocore
# used to absorb it.
ADMISSION_REFUSED_CODES = frozenset({'TooManyRequestsException', 'EC2ThrottledException'})
ADMISSION_RETRY_ATTEMPTS = 5
ADMISSION_BACKOFF_BASE_SECONDS = 1


def invoke_read_timeout(client, fn: LambdaConfig, run_async: bool = False) -> int:
    """How long this invocation may legitimately take before the socket has outlived its answer.

    Three cases, and they are not variations of one number. An Event invoke waits on Lambda taking
    the event, which is unrelated to the function. A durable invoke waits for the whole execution,
    which Lambda caps at 15 minutes whatever the function's Timeout says. Everything else waits for
    the function, so it is read from the function — a constant would be wrong in both directions,
    hanging a three-second function for the ceiling and cutting a slow one short."""
    if run_async:
        return EVENT_ACK_TIMEOUT_SECONDS
    if fn.durable:
        return DURABLE_SYNC_CAP_SECONDS + INVOKE_TIMEOUT_MARGIN_SECONDS
    try:
        configured = client.get_function_configuration(FunctionName=fn.name)['Timeout']
    except ClientError as exc:
        # Invoke and GetFunctionConfiguration are separate permissions, so a caller can hold the
        # first without the second. Falling back errs the only safe way: the worst case is waiting
        # longer than needed for a call that is already stuck.
        fallback = LAMBDA_MAX_TIMEOUT_SECONDS + INVOKE_TIMEOUT_MARGIN_SECONDS
        code = exc.response.get('Error', {}).get('Code', 'unknown')
        warn(f'could not read the Timeout of {fn.name} ({code}); waiting {fallback}s, the Lambda ceiling')
        return fallback
    return configured + INVOKE_TIMEOUT_MARGIN_SECONDS


def make_invoke_client(session: boto3.Session, read_timeout: int):
    """A lambda client for Invoke alone: it waits the function out, and it never retries.

    `total_max_attempts` counts the initial request, so 1 is exactly one call. `max_attempts` is the
    other key botocore accepts and it counts retries *on top of* the initial request, so 1 there
    still permits a duplicate invocation. The mode is pinned because legacy mode's retry set
    differs, and an explicit Config beats AWS_MAX_ATTEMPTS and AWS_RETRY_MODE in the environment."""
    return session.client(
        'lambda',
        config=Config(
            connect_timeout=INVOKE_CONNECT_TIMEOUT_SECONDS,
            read_timeout=read_timeout,
            retries={'mode': 'standard', 'total_max_attempts': 1},
        ),
    )


def admission_refused(exc: ClientError) -> bool:
    """Whether Lambda declined to start an execution, rather than failing one it had started."""
    code = exc.response.get('Error', {}).get('Code', '')
    status = exc.response.get('ResponseMetadata', {}).get('HTTPStatusCode')
    return code in ADMISSION_REFUSED_CODES or status == 429


def issue_invoke(client, sleep=time.sleep, **kwargs) -> dict:
    """Send one Invoke, re-issuing only where Lambda refused to start an execution.

    The client retries nothing, which is what stops a slow function being run five times over. That
    also drops the retries which never could duplicate a run, and a concurrency throttle is the
    common one — so it is re-issued here instead, where the safety can be argued rather than
    configured. Every other failure either started an execution or may have, and none is re-sent."""
    for attempt in range(1, ADMISSION_RETRY_ATTEMPTS + 1):
        try:
            return client.invoke(**kwargs)
        except ClientError as exc:
            if not admission_refused(exc) or attempt == ADMISSION_RETRY_ATTEMPTS:
                raise
            delay = ADMISSION_BACKOFF_BASE_SECONDS * 2 ** (attempt - 1)
            code = exc.response.get('Error', {}).get('Code', 'unknown')
            # Said out loud because the wait is otherwise indistinguishable from a slow function.
            warn(f'{code} on attempt {attempt}; nothing started, retrying in {delay}s')
            sleep(delay)
    raise AssertionError('unreachable: the loop either returns or raises')


def timed_out_message(function_name: str, read_timeout: int, run_async: bool) -> str:
    """What a socket that outlived its answer means, which differs by what was being waited for."""
    if run_async:
        return f'{function_name} did not acknowledge the event in {read_timeout}s — it may or may not be queued, and dectl did not retry'
    return f'{function_name} answered nothing in {read_timeout}s — it is still running, and dectl did not retry'


def unreachable_message(function_name: str, exc: ConnectTimeoutError | EndpointConnectionError) -> str:
    """A connection that never opened, which is the one failure that certainly invoked nothing."""
    return f'could not reach Lambda to invoke {function_name} ({type(exc).__name__}) — nothing ran'
