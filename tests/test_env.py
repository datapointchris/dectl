import pytest
from typer.testing import CliRunner

from dectl.config import GlueJobConfig
from dectl.env import active_environment
from dectl.env import contains_env_placeholder
from dectl.env import describe_active_environment
from dectl.env import render_env_model
from dectl.env import set_active_environment
from dectl.env import substitute_env
from dectl.main import app

runner = CliRunner()


@pytest.fixture(autouse=True)
def restore_active_environment():
    """active_environment is process-global, so a test that sets it would otherwise leak the
    explicit-source state into every later test file and trip the no-op-env warning there."""
    saved = (active_environment.name, active_environment.source, active_environment.warned_about_missing_placeholder)
    yield
    active_environment.name, active_environment.source, active_environment.warned_about_missing_placeholder = saved


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


def hardcoded_job() -> GlueJobConfig:
    """A job whose names spell out an environment instead of using the {env} token."""
    return GlueJobConfig(
        name='salesdata-dev-job',
        script_bucket='scripts-dev',
        scripts=['s.py'],
        role='arn:aws:iam::123456789012:role/salesdata-dev-glue-role',
    )


def test_contains_env_placeholder_searches_nested_values():
    assert contains_env_placeholder({'a': ['x', {'b': 'src-{env}'}]})
    assert not contains_env_placeholder({'a': ['x', {'b': 'src-dev'}]})


def test_explicit_env_that_substitutes_nothing_warns(capsys):
    set_active_environment('uat', '--env')
    render_env_model(hardcoded_job())

    captured = capsys.readouterr()
    assert 'changed nothing' in captured.err
    assert '--env=uat' in captured.err
    # stdout stays clean so --json output and eval'd `s3 export` are never corrupted.
    assert captured.out == ''


def test_warning_is_emitted_once_per_invocation(capsys):
    set_active_environment('uat', 'DECTL_ENV')
    render_env_model(hardcoded_job())
    render_env_model(hardcoded_job())

    assert capsys.readouterr().err.count('changed nothing') == 1


def test_no_warning_when_the_placeholder_is_present(capsys):
    set_active_environment('uat', '--env')
    render_env_model(GlueJobConfig(name='salesdata-{env}-job', script_bucket='b', scripts=['s.py'], role='r'))

    assert capsys.readouterr().err == ''


def test_no_warning_when_the_env_was_not_explicitly_requested(capsys):
    # A config default or the built-in 'dev' carries no intent to switch environments, so a
    # config without placeholders is a legitimate single-environment setup, not a mistake.
    set_active_environment('dev', 'config')
    render_env_model(hardcoded_job())

    assert capsys.readouterr().err == ''
