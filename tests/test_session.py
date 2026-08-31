import socket
import threading

import boto3
import pytest
from botocore.config import Config
from botocore.exceptions import ClientError
from botocore.exceptions import ReadTimeoutError

from dectl.config import DectlConfig
from dectl.config import LambdaConfig
from dectl.session import DURABLE_SYNC_CAP_SECONDS
from dectl.session import INVOKE_TIMEOUT_MARGIN_SECONDS
from dectl.session import invoke_read_timeout
from dectl.session import make_invoke_client
from dectl.session import make_session

CEILING = DURABLE_SYNC_CAP_SECONDS + INVOKE_TIMEOUT_MARGIN_SECONDS


def lambda_config(**overrides) -> LambdaConfig:
    return LambdaConfig.model_validate({'name': 'fn', 'source_dir': 'code'} | overrides)


class FakeLambdaConfigReader:
    """Answers GetFunctionConfiguration, or refuses it the way a caller without the permission is refused."""

    def __init__(self, timeout: int | None = None, error_code: str | None = None) -> None:
        self.timeout = timeout
        self.error_code = error_code
        self.calls: list[str] = []

    def get_function_configuration(self, FunctionName: str) -> dict:
        self.calls.append(FunctionName)
        if self.error_code:
            raise ClientError({'Error': {'Code': self.error_code, 'Message': 'nope'}}, 'GetFunctionConfiguration')
        return {'Timeout': self.timeout, 'FunctionName': FunctionName}


@pytest.fixture
def hanging_endpoint():
    """A socket that accepts connections and answers none, counting the requests that arrive.

    Lambda holds the connection open for the whole of a synchronous invocation and sends nothing
    until the function returns, so this is the shape of a slow invoke as botocore sees it. No fake
    can stand in here: botocore's retry loop sits below the client object, so a duplicate invoke
    is issued without the code under test being called twice."""
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
    # The whole point of the fix, asserted where it is decided: in botocore's retry loop, against a
    # server that behaves like a function still running.
    endpoint, connections = hanging_endpoint

    invoke_against(endpoint, {'mode': 'standard', 'total_max_attempts': 1})

    assert len(connections) == 1


def test_max_attempts_of_one_still_issues_a_duplicate_invoke(hanging_endpoint):
    # total_max_attempts counts the initial request; max_attempts counts retries on top of it. The
    # two read alike and differ by exactly the duplicate invocation this repo shipped an outage on,
    # so the wrong key is pinned here rather than left to be rediscovered.
    endpoint, connections = hanging_endpoint

    invoke_against(endpoint, {'mode': 'standard', 'max_attempts': 1})

    assert len(connections) == 2


def test_the_invoke_client_carries_no_retries_and_the_read_timeout_it_was_given():
    session = make_session(DectlConfig.model_validate({'defaults': {'account_id': '123456789012'}, 'pipelines': {}}))

    client = make_invoke_client(session, 930)

    assert client.meta.config.retries['total_max_attempts'] == 1
    assert client.meta.config.retries['mode'] == 'standard'
    assert client.meta.config.read_timeout == 930


def test_aws_retry_environment_variables_do_not_reopen_the_retry(monkeypatch):
    # AWS_MAX_ATTEMPTS and AWS_RETRY_MODE reach a plain client, so a developer machine carrying
    # them would silently restore the duplicate invoke if the Config did not win.
    monkeypatch.setenv('AWS_MAX_ATTEMPTS', '5')
    monkeypatch.setenv('AWS_RETRY_MODE', 'legacy')
    session = make_session(DectlConfig.model_validate({'defaults': {'account_id': '123456789012'}, 'pipelines': {}}))

    client = make_invoke_client(session, 930)

    assert client.meta.config.retries['total_max_attempts'] == 1
    assert client.meta.config.retries['mode'] == 'standard'


def test_a_session_client_still_retries_its_reads():
    # Retries are correct everywhere else in the tool: a retried list or filter costs a round trip.
    # Putting the invoke Config on the session would strip them from every read.
    session = make_session(DectlConfig.model_validate({'defaults': {'account_id': '123456789012'}, 'pipelines': {}}))

    client = session.client('lambda', aws_access_key_id='x', aws_secret_access_key='y')

    assert client.meta.config.retries.get('total_max_attempts', 5) > 1


def test_the_read_timeout_follows_the_function_timeout():
    reader = FakeLambdaConfigReader(timeout=3)

    assert invoke_read_timeout(reader, lambda_config()) == 3 + INVOKE_TIMEOUT_MARGIN_SECONDS
    assert reader.calls == ['fn']


def test_a_function_at_the_lambda_maximum_gets_a_wait_above_it():
    reader = FakeLambdaConfigReader(timeout=900)

    assert invoke_read_timeout(reader, lambda_config()) > 900


def test_a_durable_function_waits_the_execution_ceiling_without_asking():
    # A durable invoke waits for the whole execution rather than one invocation, and Lambda caps
    # that at 15 minutes whatever the function's Timeout says — so reading it would mislead.
    reader = FakeLambdaConfigReader(timeout=3)

    assert invoke_read_timeout(reader, lambda_config(durable=True)) == CEILING
    assert reader.calls == []


def test_an_unreadable_timeout_falls_back_to_the_ceiling_and_says_so(capsys):
    # Invoke and GetFunctionConfiguration are separate permissions. Losing the command over the
    # second would be a worse failure than waiting longer than needed.
    reader = FakeLambdaConfigReader(error_code='AccessDeniedException')

    assert invoke_read_timeout(reader, lambda_config()) == CEILING
    assert 'AccessDeniedException' in capsys.readouterr().err


def test_the_fallback_warning_stays_off_stdout(capsys):
    # stdout belongs to the answer, and every read verb takes --json.
    reader = FakeLambdaConfigReader(error_code='ResourceNotFoundException')

    invoke_read_timeout(reader, lambda_config())

    assert capsys.readouterr().out == ''
