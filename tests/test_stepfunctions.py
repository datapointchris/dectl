from typer.testing import CliRunner

from dectl.commands.stepfunctions import make_sfn_app
from dectl.commands.stepfunctions import state_machine_arn
from dectl.config import DectlConfig
from dectl.env import active_environment
from dectl.env import render_env_model

runner = CliRunner()


def make_config(step_functions: dict) -> DectlConfig:
    return DectlConfig.model_validate(
        {
            'defaults': {'account_id': '123456789012', 'region': 'us-east-2'},
            'pipelines': {'proj': {'step_functions': step_functions}},
        }
    )


def test_state_machine_arn_built_from_defaults():
    config = make_config({'flow': {'name': 'my-flow'}})
    sfn = config.pipelines['proj'].step_functions['flow']
    assert state_machine_arn(config, sfn) == 'arn:aws:states:us-east-2:123456789012:stateMachine:my-flow'


def test_state_machine_arn_uses_env_substituted_name(monkeypatch):
    monkeypatch.setattr(active_environment, 'name', 'prod')
    config = make_config({'flow': {'name': 'salesdata-{env}-flow'}})
    sfn = render_env_model(config.pipelines['proj'].step_functions['flow'])
    assert state_machine_arn(config, sfn) == 'arn:aws:states:us-east-2:123456789012:stateMachine:salesdata-prod-flow'


def test_sfn_watch_rejects_unknown_alias():
    config = make_config({'flow': {'name': 'my-flow'}})
    app = make_sfn_app('proj', config.pipelines['proj'], config)

    result = runner.invoke(app, ['watch', 'nope'])

    assert result.exit_code == 1
    assert 'unknown state machine' in result.stdout
