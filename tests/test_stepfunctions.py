from dectl.commands.stepfunctions import make_sfn_app
from dectl.commands.stepfunctions import state_machine_arn
from dectl.config import DectlConfig
from dectl.env import active_environment
from dectl.env import render_env_model
from tests.conftest import RefusalRunner

runner = RefusalRunner()


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


def test_sfn_unknown_alias_is_not_a_command():
    # The alias is a sub-app now, so an unknown machine is an unknown command (Typer usage error),
    # not a resolve_* failure. The configured aliases are discoverable via no-args / --help.
    config = make_config({'flow': {'name': 'my-flow'}})
    app = make_sfn_app('proj', config.pipelines['proj'], config)

    result = runner.invoke(app, ['nope', 'run'])

    assert result.exit_code != 0


def test_sfn_machine_exposes_run_logs_runs_verbs():
    config = make_config({'flow': {'name': 'my-flow'}})
    app = make_sfn_app('proj', config.pipelines['proj'], config)

    result = runner.invoke(app, ['flow', '--help'])

    assert result.exit_code == 0
    for verb in ('run', 'logs', 'runs'):
        assert verb in result.stdout
