import io
import json
import zipfile
from datetime import datetime
from types import SimpleNamespace

import pytest
import typer
from botocore.exceptions import ClientError
from botocore.exceptions import ReadTimeoutError
from typer.testing import CliRunner

from dectl.commands.lambda_ import make_lambda_app
from dectl.commands.lambda_ import zip_lambda
from dectl.config import DectlConfig
from dectl.config import PipelineConfig
from dectl.config import pipeline_path_faults
from dectl.config import resolve_from_root
from dectl.invoke import DURABLE_SYNC_CAP_SECONDS
from dectl.invoke import EVENT_ACK_TIMEOUT_SECONDS
from dectl.invoke import INVOKE_TIMEOUT_MARGIN_SECONDS
from dectl.invoke import timed_out_message
from dectl.paths import PathSite

runner = CliRunner()

# Executions are keyed by the resolved version, never the alias — an alias name never appears in
# a durable execution ARN.
DURABLE_ARN = 'arn:aws:lambda:us-east-2:1:function:fn:7/durable-execution/abc/x'
DURABLE_EXECUTION = {
    'DurableExecutionArn': DURABLE_ARN,
    'DurableExecutionName': 'order-1',
    'Status': 'SUCCEEDED',
    'StartTimestamp': datetime(2026, 7, 30, 12),
    'EndTimestamp': datetime(2026, 7, 30, 12, 1),
}


def make_config(lambdas: dict) -> DectlConfig:
    return DectlConfig.model_validate({'defaults': {'account_id': '123456789012'}, 'pipelines': {'proj': {'lambdas': lambdas}}})


def test_function_exposes_run_deploy_logs_verbs():
    config = make_config({'notifier': {'name': 'salesdata-{env}-notifier', 'source_dir': 'code'}})
    app = make_lambda_app('proj', config.pipelines['proj'], config)

    result = runner.invoke(app, ['notifier', '--help'])

    assert result.exit_code == 0
    for verb in ('run', 'deploy', 'logs'):
        assert verb in result.stdout


def test_unknown_function_is_not_a_command():
    config = make_config({'notifier': {'name': 'x', 'source_dir': 'code'}})
    app = make_lambda_app('proj', config.pipelines['proj'], config)

    result = runner.invoke(app, ['nope', 'run'])

    assert result.exit_code != 0


def stderr_text(result) -> str:
    """Refusals as one line. rich soft-wraps to the console width, so a phrase splits across a
    newline at one width and not another; the assertion is about the words, not the wrapping."""
    return ' '.join(result.stderr.split())


def plain_app(**overrides):
    fn = {'name': 'fn', 'source_dir': 'code'} | overrides
    config = make_config({'notifier': fn})
    return make_lambda_app('proj', config.pipelines['proj'], config)


class FakeLambda:
    def __init__(self, payload: bytes = b'{}', timeout: int = 3) -> None:
        self.invoke_kwargs: dict = {}
        self.payload = payload
        self.timeout = timeout

    def invoke(self, **kwargs):
        self.invoke_kwargs = kwargs
        return {'Payload': FakeEmptyPayload(self.payload)}

    def get_function_configuration(self, FunctionName):
        return {'Timeout': self.timeout, 'FunctionName': FunctionName}


def test_run_json_emits_clean_response(monkeypatch):
    client = FakeLambda(payload=json.dumps({'ok': True}).encode())
    patch_session(monkeypatch, client)

    result = runner.invoke(plain_app(), ['notifier', 'run', '--json'])

    assert result.exit_code == 0
    assert client.invoke_kwargs['Payload'] == b'{}'  # default payload when none supplied
    assert json.loads(result.stdout) == {'ok': True}


def durable_app(**overrides):
    fn = {'name': 'fn', 'source_dir': 'code', 'live_alias': 'live', 'durable': True} | overrides
    config = make_config({'workflow': fn})
    return make_lambda_app('proj', config.pipelines['proj'], config)


class FakeDurableLambda:
    def __init__(self, payload: bytes = b'') -> None:
        self.invoke_kwargs: dict = {}
        self.list_kwargs: dict = {}
        self.payload = payload

    def invoke(self, **kwargs):
        self.invoke_kwargs = kwargs
        return {'DurableExecutionArn': DURABLE_ARN, 'Payload': FakeEmptyPayload(self.payload)}

    def get_alias(self, FunctionName, Name):
        return {'FunctionVersion': '7'}

    def list_versions_by_function(self, **kwargs):
        return {'Versions': [{'Version': '$LATEST'}, {'Version': '7'}]}

    def list_durable_executions_by_function(self, **kwargs):
        self.list_kwargs = kwargs
        return {'DurableExecutions': [DURABLE_EXECUTION]}

    def get_durable_execution(self, **kwargs):
        return DURABLE_EXECUTION


class FakeEmptyPayload:
    def __init__(self, body: bytes = b'') -> None:
        self.body = body

    def read(self):
        return self.body


def reject_retrying_invoke(config) -> None:
    """Refuse an Invoke issued through a client botocore would retry.

    Invoke is the one call dectl makes that AWS cannot take back: a retry is a second run of a
    function Lambda cannot cancel, so the copies overlap and race each other over whatever the
    function writes. A fake that accepts any client makes the retrying invoke and the fixed one
    indistinguishable, which is how a 60-second default reached a 205-second function."""
    attempts = None if config is None else config.retries.get('total_max_attempts')
    if attempts != 1:
        raise AssertionError(
            f'invoke was issued through a client that retries (total_max_attempts={attempts}); '
            'every retry is a second run of a function Lambda cannot cancel. '
            'Build the invoking client with make_invoke_client.'
        )


class BoundLambdaClient:
    """One lambda client: the shared recorder, plus the botocore Config this client was built with.

    The read client and the invoking client are separate objects built from the same session, and
    only the second may invoke. Binding the config to the client is what lets the fake tell them
    apart at the moment invoke is called."""

    def __init__(self, backend, config) -> None:
        self._backend = backend
        self.config = config

    def invoke(self, **kwargs):
        reject_retrying_invoke(self.config)
        return self._backend.invoke(**kwargs)

    def __getattr__(self, name):
        return getattr(self._backend, name)


class FakeSession:
    def __init__(self, lambda_client, logs_client=None) -> None:
        self.lambda_client = lambda_client
        self.logs_client = logs_client
        self.configs: list = []

    def client(self, name, config=None):
        if name == 'logs':
            return self.logs_client
        self.configs.append(config)
        return BoundLambdaClient(self.lambda_client, config)

    @property
    def invoke_config(self):
        """The Config of the one client built to invoke through."""
        return next(config for config in self.configs if config is not None)


def patch_session(monkeypatch, lambda_client, logs_client=None) -> FakeSession:
    session = FakeSession(lambda_client, logs_client)
    # The command lazy-imports make_session inside the function, so patching the source binding
    # takes effect at call time.
    monkeypatch.setattr('dectl.session.make_session', lambda config: session)
    return session


def test_run_waits_the_function_timeout_and_never_retries(monkeypatch):
    session = patch_session(monkeypatch, FakeLambda(timeout=120))

    result = runner.invoke(plain_app(), ['notifier', 'run'])

    assert result.exit_code == 0
    assert session.invoke_config.read_timeout == 120 + INVOKE_TIMEOUT_MARGIN_SECONDS
    assert session.invoke_config.retries['total_max_attempts'] == 1


def test_run_reads_the_timeout_through_a_client_that_is_not_the_invoking_one(monkeypatch):
    # Two clients, one session: reads keep botocore's retries, and only the invoking client drops
    # them. Collapsing them into one is what the fix exists to prevent.
    session = patch_session(monkeypatch, FakeLambda())

    result = runner.invoke(plain_app(), ['notifier', 'run'])

    assert result.exit_code == 0
    assert session.configs.count(None) == 1


def test_run_reports_a_read_timeout_instead_of_retrying(monkeypatch):
    class TimingOutLambda(FakeLambda):
        def invoke(self, **kwargs):
            raise ReadTimeoutError(endpoint_url='https://lambda.us-east-2.amazonaws.com')

    patch_session(monkeypatch, TimingOutLambda(timeout=120))

    result = runner.invoke(plain_app(), ['notifier', 'run'])

    assert result.exit_code == 1
    # Asserted against the builder rather than its words, so rewording the refusal moves both.
    message = stderr_text(result)
    assert timed_out_message('fn', 150, run_async=False) in message
    # A still-running function's output lands in logs, which is the command that changes this.
    assert 'dectl proj lambda notifier logs' in message


def test_durable_function_swaps_run_and_logs_for_execution_verbs():
    result = runner.invoke(durable_app(), ['workflow', '--help'])

    assert result.exit_code == 0
    for verb in ('executions', 'history', 'logs', 'run', 'deploy'):
        assert verb in result.stdout


def test_ordinary_function_has_no_execution_verbs():
    config = make_config({'notifier': {'name': 'fn', 'source_dir': 'code'}})
    app = make_lambda_app('proj', config.pipelines['proj'], config)

    result = runner.invoke(app, ['notifier', 'executions'])

    assert result.exit_code != 0


def test_durable_run_qualifies_the_invocation(monkeypatch):
    # Lambda rejects an unqualified invoke of a durable function outright, because an execution is
    # pinned to the version it starts on.
    client = FakeDurableLambda()
    patch_session(monkeypatch, client)

    result = runner.invoke(durable_app(), ['workflow', 'run'])

    assert result.exit_code == 0
    assert client.invoke_kwargs['Qualifier'] == 'live'
    assert 'InvocationType' not in client.invoke_kwargs
    assert DURABLE_ARN in result.stdout


def test_durable_run_async_names_the_execution(monkeypatch):
    client = FakeDurableLambda()
    patch_session(monkeypatch, client)

    result = runner.invoke(durable_app(), ['workflow', 'run', '--async', '--name', 'order-1'])

    assert result.exit_code == 0
    assert client.invoke_kwargs['InvocationType'] == 'Event'
    assert client.invoke_kwargs['DurableExecutionName'] == 'order-1'


def test_durable_run_waits_the_execution_ceiling(monkeypatch):
    # A durable invoke waits for the whole execution, which Lambda caps at 15 minutes whatever the
    # function's own Timeout says.
    session = patch_session(monkeypatch, FakeDurableLambda())

    result = runner.invoke(durable_app(), ['workflow', 'run'])

    assert result.exit_code == 0
    assert session.invoke_config.read_timeout == DURABLE_SYNC_CAP_SECONDS + INVOKE_TIMEOUT_MARGIN_SECONDS


def test_durable_async_run_also_refuses_to_retry(monkeypatch):
    # An Event invoke returns before any read timeout can expire, but a retried one starts a second
    # execution whenever --name was not given.
    session = patch_session(monkeypatch, FakeDurableLambda())

    result = runner.invoke(durable_app(), ['workflow', 'run', '--async'])

    assert result.exit_code == 0
    assert session.invoke_config.retries['total_max_attempts'] == 1


def test_durable_async_run_waits_on_the_acknowledgement_not_the_execution(monkeypatch):
    # The wait is resolved after --async is read. Handing an Event invoke the synchronous ceiling
    # blocks the command for a quarter of an hour on a stalled endpoint, for a call taking
    # milliseconds — and the refusal would claim an execution is running that may never have queued.
    session = patch_session(monkeypatch, FakeDurableLambda())

    result = runner.invoke(durable_app(), ['workflow', 'run', '--async'])

    assert result.exit_code == 0
    assert session.invoke_config.read_timeout == EVENT_ACK_TIMEOUT_SECONDS


def test_durable_async_run_reports_a_timeout_without_claiming_the_execution_started(monkeypatch):
    class TimingOutDurableLambda(FakeDurableLambda):
        def invoke(self, **kwargs):
            raise ReadTimeoutError(endpoint_url='https://lambda.us-east-2.amazonaws.com')

    patch_session(monkeypatch, TimingOutDurableLambda())

    result = runner.invoke(durable_app(), ['workflow', 'run', '--async'])

    assert result.exit_code == 1
    assert timed_out_message('fn', EVENT_ACK_TIMEOUT_SECONDS, run_async=True) in stderr_text(result)


def test_a_throttled_run_is_sent_again_and_succeeds(monkeypatch):
    # The invoking client retries nothing, so the throttle Lambda used to absorb has to be re-issued
    # here or a function at its concurrency limit fails the command with a traceback.
    class ThrottledOnceLambda(FakeDurableLambda):
        def __init__(self) -> None:
            super().__init__()
            self.attempts = 0

        def invoke(self, **kwargs):
            self.attempts += 1
            if self.attempts == 1:
                response = {
                    'Error': {'Code': 'TooManyRequestsException', 'Message': 'Rate Exceeded.'},
                    'ResponseMetadata': {'HTTPStatusCode': 429},
                }
                raise ClientError(response, 'Invoke')
            return super().invoke(**kwargs)

    client = ThrottledOnceLambda()
    patch_session(monkeypatch, client)
    monkeypatch.setattr('time.sleep', lambda seconds: None)

    result = runner.invoke(durable_app(), ['workflow', 'run'])

    assert result.exit_code == 0
    assert client.attempts == 2


def test_durable_run_reports_a_read_timeout_and_points_at_executions(monkeypatch):
    class TimingOutDurableLambda(FakeDurableLambda):
        def invoke(self, **kwargs):
            raise ReadTimeoutError(endpoint_url='https://lambda.us-east-2.amazonaws.com')

    patch_session(monkeypatch, TimingOutDurableLambda())

    result = runner.invoke(durable_app(), ['workflow', 'run'])

    assert result.exit_code == 1
    message = stderr_text(result)
    assert timed_out_message('fn', DURABLE_SYNC_CAP_SECONDS + INVOKE_TIMEOUT_MARGIN_SECONDS, run_async=False) in message
    # The execution outlives the socket, so the listing is where its outcome is read.
    assert 'dectl proj lambda workflow executions' in message


def test_durable_run_without_a_live_alias_falls_back_to_latest(monkeypatch):
    client = FakeDurableLambda()
    patch_session(monkeypatch, client)

    result = runner.invoke(durable_app(live_alias=None), ['workflow', 'run'])

    assert result.exit_code == 0
    assert client.invoke_kwargs['Qualifier'] == '$LATEST'


def test_durable_run_json_keeps_the_execution_arn_off_stdout(monkeypatch):
    # The ARN line is human orientation; emitting it alongside --json would break a jq pipe.
    client = FakeDurableLambda(payload=json.dumps({'ok': True}).encode())
    patch_session(monkeypatch, client)

    result = runner.invoke(durable_app(), ['workflow', 'run', '--json'])

    assert result.exit_code == 0
    assert json.loads(result.stdout) == {'ok': True}


def test_executions_json_emits_a_stable_shape(monkeypatch):
    patch_session(monkeypatch, FakeDurableLambda())

    result = runner.invoke(durable_app(), ['workflow', 'executions', '--json'])

    assert result.exit_code == 0
    assert json.loads(result.stdout) == [
        {
            'name': 'order-1',
            'status': 'SUCCEEDED',
            'version': '7',
            'started': '2026-07-30 12:00:00',
            'ended': '2026-07-30 12:01:00',
            'arn': DURABLE_ARN,
        }
    ]


def test_executions_resolves_the_alias_before_listing(monkeypatch):
    # The bug: listing with Qualifier=live fails with "cannot filter durable executions by alias",
    # because Lambda resolves the alias to a version when the execution starts and the alias name
    # never appears in the execution ARN.
    client = FakeDurableLambda()
    patch_session(monkeypatch, client)

    result = runner.invoke(durable_app(), ['workflow', 'executions'])

    assert result.exit_code == 0
    assert client.list_kwargs['Qualifier'] == '7'
    # The resolution is shown, so a list emptied by a deploy is not silently mysterious.
    assert 'live' in result.stdout
    assert '7' in result.stdout


def test_executions_across_versions_names_what_it_scanned(monkeypatch):
    client = FakeDurableLambda()
    patch_session(monkeypatch, client)

    result = runner.invoke(durable_app(), ['workflow', 'executions', '--all-versions'])

    assert result.exit_code == 0
    assert 'versions' in result.stdout


def test_durable_logs_scope_to_the_resolved_execution(monkeypatch):
    lambda_client = FakeDurableLambda()

    class FakeLogs:
        def __init__(self) -> None:
            self.calls: list[dict] = []

        def filter_log_events(self, **kwargs):
            self.calls.append(kwargs)
            return {'events': []}

    logs_client = FakeLogs()
    patch_session(monkeypatch, lambda_client, logs_client)

    result = runner.invoke(durable_app(), ['workflow', 'logs'])

    assert result.exit_code == 0
    # The SDK logger stamps the execution ARN on every record; that is the filter the console's
    # "Logger output" tab uses and the only thing separating interleaved executions in one group.
    assert logs_client.calls[0]['filterPattern'] == f'"{DURABLE_ARN}"'
    assert logs_client.calls[0]['logGroupName'] == '/aws/lambda/fn'
    assert logs_client.calls[0]['endTime'] > logs_client.calls[0]['startTime']


def test_zip_holds_the_resolved_directorys_files_at_the_archive_root(tmp_path):
    source = tmp_path / 'checkout' / 'modules' / 'code'
    source.mkdir(parents=True)
    (source / 'handler.py').write_text('def handler(event, context): pass')
    (source / 'vendor').mkdir()
    (source / 'vendor' / 'lib.py').write_text('x = 1')
    pipeline = PipelineConfig.model_validate(
        {'resolve_paths_from': str(tmp_path / 'checkout'), 'lambdas': {'fn': {'name': 'n', 'source_dir': 'modules/code'}}}
    )

    zip_path = zip_lambda(resolve_from_root(pipeline, pipeline.lambdas['fn'].source_dir))

    with zipfile.ZipFile(zip_path) as archive:
        assert sorted(archive.namelist()) == ['handler.py', 'vendor/lib.py']


class RecordingLambdaClient:
    """Enough of the Lambda API for a deploy, recording what it was sent."""

    def __init__(self) -> None:
        self.code: bytes | None = None
        self.published: list[str] = []
        self.aliases: list[tuple[str, str]] = []

    def update_function_code(self, FunctionName, ZipFile):
        self.code = ZipFile

    def get_waiter(self, name):
        # Real botocore raises `ValueError: Waiter does not exist` for a name it does not know,
        # so a fake handing back a waiter for anything lets a wrong name ship green. `deploy
        # --publish` passes the version-suffixed one, which is exactly where a typo would live.
        if name != 'function_updated_v2':
            raise ValueError(f'Waiter does not exist: {name}')
        return SimpleNamespace(wait=lambda **kwargs: None)

    def publish_version(self, FunctionName):
        self.published.append(FunctionName)
        return {'Version': '7'}

    def update_alias(self, FunctionName, Name, FunctionVersion):
        self.aliases.append((Name, FunctionVersion))


def deploy_app(monkeypatch, tmp_path, client, source_dir: str = 'code'):
    config = DectlConfig.model_validate(
        {
            'defaults': {'account_id': '1'},
            'pipelines': {
                'proj': {
                    'resolve_paths_from': str(tmp_path),
                    'lambdas': {'notifier': {'name': 'salesdata-{env}-notifier', 'source_dir': source_dir, 'live_alias': 'live'}},
                }
            },
        }
    )
    monkeypatch.setattr('dectl.session.make_session', lambda _config: FakeSession(client))
    return make_lambda_app('proj', config.pipelines['proj'], config)


def test_lambda_deploy_publishes_the_zip_and_moves_the_alias(monkeypatch, tmp_path):
    # The write path, end to end. Replacing `resolve_from_root(...)` with `Path(fn.source_dir)`
    # left the whole suite green, and that mutation restores the pre-branch behaviour of the
    # feature's headline case: the deploy reading from wherever it was invoked.
    (tmp_path / 'code').mkdir()
    (tmp_path / 'code' / 'handler.py').write_text('def handler(event, context): pass')
    client = RecordingLambdaClient()
    app = deploy_app(monkeypatch, tmp_path, client)

    result = runner.invoke(app, ['notifier', 'deploy', '--publish'], catch_exceptions=False)

    assert result.exit_code == 0
    with zipfile.ZipFile(io.BytesIO(client.code)) as archive:
        assert archive.namelist() == ['handler.py']
    assert client.published == ['salesdata-dev-notifier']
    assert client.aliases == [('live', '7')]


def test_lambda_deploy_refuses_a_missing_source_and_writes_nothing(monkeypatch, tmp_path):
    # A refusal dectl chose, not an exception reaching the runner: CliRunner reports 1 for both,
    # so `exit_code == 1` beside `code is None` would be satisfied by any crash before the write.
    client = RecordingLambdaClient()
    app = deploy_app(monkeypatch, tmp_path, client, source_dir='modules/absent')

    result = runner.invoke(app, ['notifier', 'deploy'])

    assert isinstance(result.exception, SystemExit)
    assert result.exit_code == 1
    assert client.code is None


def test_lambda_deploy_reports_the_config_fault_before_building_a_session(monkeypatch, tmp_path):
    # A session is built from the AWS profile, and botocore reports a missing one as a
    # traceback naming the profile. Ordering the session first meant a box missing the checkout
    # was told about its profile instead, and the machine missing one is usually missing both.
    # The fake refuses to be built for the same reason the real one does, because a fake that
    # hands back a session cannot show which of the two the door reached first.
    def refuse(_config):
        raise RuntimeError('ProfileNotFound: the config profile (no-such-profile) could not be found')

    monkeypatch.setattr('dectl.session.make_session', refuse)
    config = DectlConfig.model_validate(
        {
            'defaults': {'account_id': '1'},
            'pipelines': {
                'proj': {
                    'resolve_paths_from': str(tmp_path / 'absent-checkout'),
                    'lambdas': {'notifier': {'name': 'n', 'source_dir': 'code'}},
                }
            },
        }
    )
    app = make_lambda_app('proj', config.pipelines['proj'], config)

    result = runner.invoke(app, ['notifier', 'deploy'])

    assert isinstance(result.exception, SystemExit)
    assert result.exit_code == 1


def test_the_lambda_door_names_the_same_site_config_validate_does(tmp_path):
    # Which pipeline, which alias, which config key — asserted on the row rather than on the
    # sentence it renders to. A bare path cannot say which `notifier` it meant when several
    # pipelines carry one.
    pipeline = PipelineConfig.model_validate(
        {'resolve_paths_from': str(tmp_path), 'lambdas': {'notifier': {'name': 'n', 'source_dir': 'modules/absent'}}}
    )

    faults = pipeline_path_faults('proj', pipeline, on_disk=True, resource='lambda', alias='notifier')

    assert [(f.pipeline, f.site) for f in faults] == [('proj', PathSite('lambda', 'notifier', 'source_dir'))]


def test_pycache_is_left_out_of_the_zip(tmp_path):
    source = tmp_path / 'code'
    (source / '__pycache__').mkdir(parents=True)
    (source / 'handler.py').write_text('x')
    (source / '__pycache__' / 'handler.cpython-313.pyc').write_text('bytecode')

    zip_path = zip_lambda(source)

    with zipfile.ZipFile(zip_path) as archive:
        assert archive.namelist() == ['handler.py']


def test_zipping_a_missing_directory_exits_rather_than_shipping_an_empty_archive(tmp_path):
    with pytest.raises(typer.Exit) as exit_info:
        zip_lambda(tmp_path / 'not-here')

    assert exit_info.value.exit_code == 1


def test_zipping_a_file_exits_rather_than_shipping_an_empty_archive(tmp_path):
    # rglob on a file yields nothing, so a source_dir pointing at a file would upload a valid
    # but empty archive and replace the function's code with nothing.
    handler = tmp_path / 'handler.py'
    handler.write_text('x')

    with pytest.raises(typer.Exit) as exit_info:
        zip_lambda(handler)

    assert exit_info.value.exit_code == 1
