import json
from datetime import datetime

from typer.testing import CliRunner

from dectl.commands.lambda_ import make_lambda_app
from dectl.config import DectlConfig

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


def test_run_json_emits_clean_response(monkeypatch):
    config = make_config({'notifier': {'name': 'fn', 'source_dir': 'code'}})
    app = make_lambda_app('proj', config.pipelines['proj'], config)

    class FakePayload:
        def read(self):
            return json.dumps({'ok': True}).encode()

    class FakeLambda:
        def invoke(self, FunctionName, Payload):
            assert Payload == b'{}'  # default payload when none supplied
            return {'Payload': FakePayload()}

    class FakeSession:
        def client(self, name):
            return FakeLambda()

    # The command lazy-imports make_session inside the function, so patching the source binding
    # takes effect at call time.
    monkeypatch.setattr('dectl.session.make_session', lambda config: FakeSession())

    result = runner.invoke(app, ['notifier', 'run', '--json'])

    assert result.exit_code == 0
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


def patch_session(monkeypatch, lambda_client, logs_client=None):
    class FakeSession:
        def client(self, name):
            return logs_client if name == 'logs' else lambda_client

    monkeypatch.setattr('dectl.session.make_session', lambda config: FakeSession())


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
