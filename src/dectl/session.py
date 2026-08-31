import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

from dectl.config import DectlConfig
from dectl.config import LambdaConfig
from dectl.output import warn

# Invoke is the one call dectl makes that AWS cannot take back, so its socket has to outlive the
# function rather than the other way round. botocore's defaults are a 60-second read timeout and
# five attempts, which turn any function slower than a minute into five overlapping copies of
# itself: every retry is a fresh invocation, and Lambda cannot cancel the run already in flight.
# The margin sits above the function's own Timeout, where Lambda kills the run first, so a socket
# that does expire means the network rather than a slow handler.
INVOKE_TIMEOUT_MARGIN_SECONDS = 30

# A synchronous durable invoke waits for the whole execution rather than one invocation, and
# Lambda caps that wait at 15 minutes whatever the function's Timeout says. It is also the ceiling
# for any function, which makes it the safe answer when the real Timeout cannot be read.
DURABLE_SYNC_CAP_SECONDS = 900

# Establishing the connection is not the part that takes minutes, and a connection that has not
# opened has invoked nothing, so this stays short.
INVOKE_CONNECT_TIMEOUT_SECONDS = 10


def make_session(config: DectlConfig) -> boto3.Session:
    kwargs: dict = {'region_name': config.defaults.region}
    if config.defaults.aws_profile:
        kwargs['profile_name'] = config.defaults.aws_profile
    return boto3.Session(**kwargs)


def invoke_read_timeout(client, fn: LambdaConfig) -> int:
    """How long a synchronous invoke of this function may legitimately take.

    Read from the function rather than fixed, because a fixed number is wrong in both directions:
    too low re-opens the retry storm, and too high makes a three-second function hang for the
    fifteen-minute ceiling when its socket dies. A durable function is the exception — the wait is
    for the whole execution, which Lambda caps at that ceiling regardless of Timeout."""
    if fn.durable:
        return DURABLE_SYNC_CAP_SECONDS + INVOKE_TIMEOUT_MARGIN_SECONDS
    try:
        configured = client.get_function_configuration(FunctionName=fn.name)['Timeout']
    except ClientError as exc:
        # Invoke and GetFunctionConfiguration are separate permissions, so a caller can hold the
        # first without the second. Falling back to the ceiling errs the only safe way: the worst
        # case is waiting longer than needed for a call that is already stuck.
        code = exc.response.get('Error', {}).get('Code', 'unknown')
        warn(f'could not read the Timeout of {fn.name} ({code}); waiting the {DURABLE_SYNC_CAP_SECONDS}s Lambda ceiling')
        return DURABLE_SYNC_CAP_SECONDS + INVOKE_TIMEOUT_MARGIN_SECONDS
    return configured + INVOKE_TIMEOUT_MARGIN_SECONDS


def make_invoke_client(session: boto3.Session, read_timeout: int):
    """A lambda client for Invoke alone: it waits the function out, and it never retries.

    `total_max_attempts` counts the initial request, so 1 is exactly one call. `max_attempts` is
    the other key botocore accepts and it counts retries *on top of* the initial request, so 1
    there still permits the duplicate invocation this exists to prevent. The mode is pinned
    because legacy mode's retry set differs, and an explicit Config beats AWS_MAX_ATTEMPTS and
    AWS_RETRY_MODE in the environment."""
    return session.client(
        'lambda',
        config=Config(
            connect_timeout=INVOKE_CONNECT_TIMEOUT_SECONDS,
            read_timeout=read_timeout,
            retries={'mode': 'standard', 'total_max_attempts': 1},
        ),
    )
