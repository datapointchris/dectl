from typer.testing import CliRunner

from dectl.config import GlueJobConfig
from dectl.env import active_environment
from dectl.env import describe_active_environment
from dectl.env import render_env_model
from dectl.env import set_active_environment
from dectl.env import substitute_env
from dectl.main import app

runner = CliRunner()


def test_substitute_env_replaces_only_the_token(monkeypatch):
    monkeypatch.setattr(active_environment, 'name', 'prod')
    assert substitute_env('salesdata-{env}-ds-do-the-thing-lambda') == 'salesdata-prod-ds-do-the-thing-lambda'
    # Braces that are not the {env} token (e.g. a JSON payload) are left alone.
    assert substitute_env('{"key": "value"} for {env}') == '{"key": "value"} for prod'


def test_render_env_model_substitutes_every_string_field_without_mutating_original(monkeypatch):
    monkeypatch.setattr(active_environment, 'name', 'staging')
    job = GlueJobConfig(
        name='salesdata-{env}-job',
        script_bucket='scripts-{env}',
        scripts=['s.py'],
        role='arn:aws:iam::123456789012:role/salesdata-{env}-glue-role',
        arguments={'SOURCE_BUCKET': 'src-{env}'},
    )
    rendered = render_env_model(job)

    assert rendered.name == 'salesdata-staging-job'
    assert rendered.script_bucket == 'scripts-staging'
    assert rendered.role == 'arn:aws:iam::123456789012:role/salesdata-staging-glue-role'
    assert rendered.arguments['SOURCE_BUCKET'] == 'src-staging'
    # The stored config keeps its placeholder so a later env renders differently.
    assert job.name == 'salesdata-{env}-job'


def test_default_active_environment_is_dev():
    assert active_environment.name == 'dev'


def test_set_active_environment_records_name_and_source(monkeypatch):
    monkeypatch.setattr(active_environment, 'name', 'dev')
    monkeypatch.setattr(active_environment, 'source', 'default')
    set_active_environment('prod', 'DECTL_ENV')
    assert describe_active_environment() == 'prod (from DECTL_ENV)'


def test_env_command_reports_flag_as_source():
    result = runner.invoke(app, ['--env', 'prod', 'env'])
    assert result.exit_code == 0
    assert 'prod' in result.stdout
    assert '--env' in result.stdout


def test_env_command_reports_env_var_as_source():
    result = runner.invoke(app, ['env'], env={'DECTL_ENV': 'staging'})
    assert result.exit_code == 0
    assert 'staging' in result.stdout
    assert 'DECTL_ENV' in result.stdout
