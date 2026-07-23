import json

from typer.testing import CliRunner

from dectl.commands.lambda_ import make_lambda_app
from dectl.config import DectlConfig

runner = CliRunner()


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
