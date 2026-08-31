import socket
import threading

import boto3
import pytest
from botocore.config import Config
from botocore.exceptions import ClientError
from botocore.exceptions import ReadTimeoutError

from dectl.config import DectlConfig
from dectl.config import LambdaConfig
from dectl.invoke import ADMISSION_RETRY_ATTEMPTS
from dectl.invoke import DURABLE_SYNC_CAP_SECONDS
from dectl.invoke import EVENT_ACK_TIMEOUT_SECONDS
from dectl.invoke import INVOKE_TIMEOUT_MARGIN_SECONDS
from dectl.invoke import LAMBDA_MAX_TIMEOUT_SECONDS
from dectl.invoke import invoke_read_timeout
from dectl.invoke import issue_invoke
from dectl.invoke import make_invoke_client
from dectl.session import make_session

CEILING = LAMBDA_MAX_TIMEOUT_SECONDS + INVOKE_TIMEOUT_MARGIN_SECONDS


def lambda_config(**overrides) -> LambdaConfig:
    return LambdaConfig.model_validate({'name': 'fn', 'source_dir': 'code'} | overrides)


def empty_session() -> boto3.Session:
    return make_session(DectlConfig.model_validate({'defaults': {'account_id': '123456789012'}, 'pipelines': {}}))


def one_line(text: str) -> str:
    """rich soft-wraps to the console width, so a value splits across a newline at one width and
    not another."""
    return ' '.join(text.split())


def client_error(code: str, status: int = 400) -> ClientError:
    response = {'Error': {'Code': code, 'Message': 'nope'}, 'ResponseMetadata': {'HTTPStatusCode': status}}
    return ClientError(response, 'Invoke')


class FakeLambdaConfigReader:
    """Answers GetFunctionConfiguration, or refuses it the way a caller without the permission is refused."""

    def __init__(self, timeout: int | None = None, error_code: str | None = None) -> None:
        self.timeout = timeout
        self.error_code = error_code
        self.calls: list[str] = []

    def get_function_configuration(self, FunctionName: str) -> dict:
        self.calls.append(FunctionName)
        if self.error_code:
            raise client_error(self.error_code)
        return {'Timeout': self.timeout, 'FunctionName': FunctionName}


class FakeInvoker:
    """Raises a queue of failures before answering, so a caller's re-issue policy is what decides
    whether the call ever succeeds."""

    def __init__(self, failures: list[ClientError] | None = None) -> None:
        self.failures = list(failures or [])
        self.attempts = 0

    def invoke(self, **kwargs) -> dict:
        self.attempts += 1
        if self.failures:
            raise self.failures.pop(0)
        return {'Payload': None, 'kwargs': kwargs}


@pytest.fixture
def hanging_endpoint():
    """A socket that accepts connections and answers none, counting the requests that arrive.

    Lambda holds the connection open for the whole of a synchronous invocation and sends nothing
    until the function returns, so this is the shape of a slow invoke as botocore sees it. No fake
    can stand in here: botocore's retry loop sits below the client object, so a duplicate invoke is
    issued without the code under test being called twice."""
    stop = threading.Event()
    connections: list[socket.socket] = []

    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(('127.0.0.1', 0))
    server.listen(16)
    port = server.getsockname()[1]

    def accept_and_hold() -> None:
        server.settimeout(0.05)
        while not stop.is_set():
            try:
                conn, _ = server.accept()
            except TimeoutError:
                continue
            connections.append(conn)

    thread = threading.Thread(target=accept_and_hold, daemon=True)
    thread.start()

    yield f'http://127.0.0.1:{port}', connections

    stop.set()
    thread.join(timeout=2)
    for conn in connections:
        conn.close()
    server.close()


def invoke_against(endpoint: str, retries: dict) -> None:
    client = boto3.client(
        'lambda',
        region_name='us-east-1',
        endpoint_url=endpoint,
        aws_access_key_id='x',
        aws_secret_access_key='y',
        config=Config(connect_timeout=2, read_timeout=0.1, retries=retries),
    )
    with pytest.raises(ReadTimeoutError):
        client.invoke(FunctionName='fn', Payload=b'{}')


def test_the_invoke_client_issues_exactly_one_request(hanging_endpoint):
    # Asserted in requests that arrived, which is the only place it is decided. Every other test
    # here rests on total_max_attempts=1 meaning one call, and no client object can show that.
    endpoint, connections = hanging_endpoint

    invoke_against(endpoint, {'mode': 'standard', 'total_max_attempts': 1})

    assert len(connections) == 1


def test_max_attempts_of_one_still_issues_a_duplicate_invoke(hanging_endpoint):
    # total_max_attempts counts the initial request; max_attempts counts retries on top of it. The
    # two read alike and differ by exactly the duplicate invocation this repo shipped an outage on.
    endpoint, connections = hanging_endpoint

    invoke_against(endpoint, {'mode': 'standard', 'max_attempts': 1})

    assert len(connections) == 2


def test_the_invoke_client_carries_no_retries_and_the_read_timeout_it_was_given():
    client = make_invoke_client(empty_session(), 930)

    assert client.meta.config.retries['total_max_attempts'] == 1
    assert client.meta.config.retries['mode'] == 'standard'
    assert client.meta.config.read_timeout == 930


def test_aws_retry_environment_variables_do_not_reopen_the_retry(monkeypatch):
    # AWS_MAX_ATTEMPTS and AWS_RETRY_MODE reach a plain client, so a developer machine carrying them
    # would silently restore the duplicate invoke if the Config did not win.
    monkeypatch.setenv('AWS_MAX_ATTEMPTS', '5')
    monkeypatch.setenv('AWS_RETRY_MODE', 'legacy')

    client = make_invoke_client(empty_session(), 930)

    assert client.meta.config.retries['total_max_attempts'] == 1
    assert client.meta.config.retries['mode'] == 'standard'


def test_a_session_client_still_retries_its_reads():
    # Retries are correct everywhere else in the tool: a retried list or filter costs a round trip.
    client = empty_session().client('lambda', aws_access_key_id='x', aws_secret_access_key='y')

    assert client.meta.config.retries.get('total_max_attempts', 5) > 1


def test_the_read_timeout_follows_the_function_timeout():
    reader = FakeLambdaConfigReader(timeout=3)

    assert invoke_read_timeout(reader, lambda_config()) == 3 + INVOKE_TIMEOUT_MARGIN_SECONDS
    assert reader.calls == ['fn']


def test_a_function_at_the_lambda_maximum_gets_a_wait_above_it():
    reader = FakeLambdaConfigReader(timeout=LAMBDA_MAX_TIMEOUT_SECONDS)

    assert invoke_read_timeout(reader, lambda_config()) > LAMBDA_MAX_TIMEOUT_SECONDS


def test_a_durable_function_waits_the_execution_ceiling_without_asking():
    # A durable invoke waits for the whole execution, which Lambda caps at 15 minutes whatever the
    # function's Timeout says — so reading it would mislead.
    reader = FakeLambdaConfigReader(timeout=3)

    assert invoke_read_timeout(reader, lambda_config(durable=True)) == DURABLE_SYNC_CAP_SECONDS + INVOKE_TIMEOUT_MARGIN_SECONDS
    assert reader.calls == []


def test_an_event_invoke_waits_on_the_acknowledgement_not_the_function():
    # An Event invoke returns as soon as Lambda has the event. Giving it the synchronous wait blocks
    # the command for a quarter of an hour on a stalled endpoint, for a call that takes milliseconds.
    reader = FakeLambdaConfigReader(timeout=LAMBDA_MAX_TIMEOUT_SECONDS)

    assert invoke_read_timeout(reader, lambda_config(), run_async=True) == EVENT_ACK_TIMEOUT_SECONDS
    assert invoke_read_timeout(reader, lambda_config(durable=True), run_async=True) == EVENT_ACK_TIMEOUT_SECONDS
    assert reader.calls == []


def test_an_unreadable_timeout_falls_back_to_the_ceiling_and_warns_the_wait_it_takes(capsys):
    # Invoke and GetFunctionConfiguration are separate permissions. Losing the command over the
    # second would be a worse failure than waiting longer than needed. The warning has to name the
    # number actually used, or one invocation reports two different waits.
    reader = FakeLambdaConfigReader(error_code='AccessDeniedException')

    timeout = invoke_read_timeout(reader, lambda_config())

    assert timeout == CEILING
    warning = one_line(capsys.readouterr().err)
    assert 'AccessDeniedException' in warning
    assert f'{timeout}s' in warning


def test_the_fallback_warning_stays_off_stdout(capsys):
    # stdout belongs to the answer, and every read verb takes --json.
    reader = FakeLambdaConfigReader(error_code='ResourceNotFoundException')

    invoke_read_timeout(reader, lambda_config())

    assert capsys.readouterr().out == ''


def test_a_throttle_is_sent_again_because_it_started_nothing():
    # The no-retry client also drops the retries that never could duplicate a run, and a function at
    # its concurrency limit is the common one. Lambda refused the invocation, so re-sending is safe.
    invoker = FakeInvoker([client_error('TooManyRequestsException', status=429)])
    slept: list[float] = []

    issue_invoke(invoker, sleep=slept.append, FunctionName='fn', Payload=b'{}')

    assert invoker.attempts == 2
    assert slept == [1]


def test_a_throttle_backs_off_exponentially_and_then_gives_up():
    failures = [client_error('TooManyRequestsException', status=429) for _ in range(ADMISSION_RETRY_ATTEMPTS)]
    invoker = FakeInvoker(failures)
    slept: list[float] = []

    with pytest.raises(ClientError):
        issue_invoke(invoker, sleep=slept.append, FunctionName='fn', Payload=b'{}')

    assert invoker.attempts == ADMISSION_RETRY_ATTEMPTS
    assert slept == [1, 2, 4, 8]


def test_a_refusal_recognised_only_by_its_status_is_still_sent_again():
    # A 429 is Lambda declining to admit the invocation whatever it calls the code.
    invoker = FakeInvoker([client_error('SomeNewThrottleName', status=429)])

    issue_invoke(invoker, sleep=lambda seconds: None, FunctionName='fn', Payload=b'{}')

    assert invoker.attempts == 2


def test_a_failure_that_may_have_started_an_execution_is_never_sent_again():
    # The whole point: anything that might already be running is reported, not repeated.
    invoker = FakeInvoker([client_error('ServiceException', status=500)])

    with pytest.raises(ClientError):
        issue_invoke(invoker, sleep=lambda seconds: None, FunctionName='fn', Payload=b'{}')

    assert invoker.attempts == 1


def test_a_read_timeout_is_never_sent_again():
    class TimingOutInvoker:
        def __init__(self) -> None:
            self.attempts = 0

        def invoke(self, **kwargs):
            self.attempts += 1
            raise ReadTimeoutError(endpoint_url='https://lambda.us-east-2.amazonaws.com')

    invoker = TimingOutInvoker()

    with pytest.raises(ReadTimeoutError):
        issue_invoke(invoker, sleep=lambda seconds: None, FunctionName='fn', Payload=b'{}')

    assert invoker.attempts == 1


def test_a_successful_invoke_is_sent_once_and_its_arguments_survive():
    invoker = FakeInvoker()

    response = issue_invoke(invoker, FunctionName='fn', Payload=b'{"k": 1}')

    assert invoker.attempts == 1
    assert response['kwargs'] == {'FunctionName': 'fn', 'Payload': b'{"k": 1}'}
