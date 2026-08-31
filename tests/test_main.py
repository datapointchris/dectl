import pytest
from pydantic import ValidationError
from typer.testing import CliRunner

import dectl.main
from dectl.config import DectlConfig
from dectl.main import app

runner = CliRunner()


def test_reference_prints_the_grammar():
    result = runner.invoke(app, ['reference'])

    assert result.exit_code == 0
    assert 'RESOURCE ALIAS VERB' in result.stdout
    assert 'Instance verbs' in result.stdout
    assert 'Set / pipeline verbs' in result.stdout
    # Option-syntax brackets must survive verbatim, not be eaten as rich markup tags.
    assert '[--follow]' in result.stdout


def test_update_installs_the_published_release(monkeypatch):
    calls = []
    monkeypatch.setattr(dectl.main, 'run_update', lambda config, **kwargs: calls.append((config, kwargs)))

    result = runner.invoke(app, ['update', '--check'])

    assert result.exit_code == 0
    config, kwargs = calls[0]
    assert (config.tool, config.owner) == ('dectl', 'datapointchris')
    assert kwargs == {'check_only': True}


def test_update_does_not_also_print_the_update_notice(monkeypatch):
    # The notice would name the release the command is already installing.
    monkeypatch.setattr(dectl.main, 'run_update', lambda config, **kwargs: None)
    notices = []
    monkeypatch.setattr(dectl.main, 'notify', notices.append)

    runner.invoke(app, ['update'])
    runner.invoke(app, ['reference'])

    assert notices == [dectl.main.UPDATE_CONFIG]


def test_the_credential_is_left_to_pyselfupdate():
    """pyselfupdate runs `gh auth token` itself, and `$GITHUB_TOKEN_COMMAND` is
    what redirects or empties it — a lever that belongs to whoever runs dectl.

    A helper here would be a second implementation of that, and the reason the
    library grew one is that eleven tools never pasted it.
    """
    assert dectl.main.UPDATE_CONFIG.token == ''
    assert dectl.main.UPDATE_CONFIG.token_func is None


def broken_config_error() -> ValidationError:
    """The exception import-time load stores, produced the way the real one is."""
    with pytest.raises(ValidationError) as caught:
        DectlConfig.model_validate({'defaults': {'account_id': '1'}, 'pipelines': {'p': {'step_function': {}}}})
    return caught.value


@pytest.fixture
def invalid_config(monkeypatch):
    """The state import-time load leaves behind when the config file exists but does not load."""
    monkeypatch.setenv('COLUMNS', '200')
    monkeypatch.setattr(dectl.main, 'cfg', None)
    monkeypatch.setattr(dectl.main, 'CONFIG_ERROR', broken_config_error())


@pytest.fixture
def missing_config(monkeypatch):
    monkeypatch.setenv('COLUMNS', '200')
    monkeypatch.setattr(dectl.main, 'cfg', None)
    monkeypatch.setattr(dectl.main, 'CONFIG_ERROR', None)


def test_list_reports_the_validation_error_not_a_missing_config(invalid_config):
    # The config exists. Reporting it as absent points the reader at `config init`, which
    # refuses because the file it would write is already there.
    result = runner.invoke(app, ['list'])

    assert result.exit_code == 1
    assert 'does not match the expected schema' in result.stderr
    assert 'pipelines.p.step_function' in result.stderr
    assert 'config init' not in result.stderr


def test_list_still_reports_a_missing_config_as_missing(missing_config):
    result = runner.invoke(app, ['list'])

    assert result.exit_code == 1
    assert 'no config found' in result.stderr
    assert 'config init' in result.stderr


def test_search_reports_the_validation_error_before_touching_aws(invalid_config):
    # No session is built, so the refusal does not depend on credentials being present.
    result = runner.invoke(app, ['search', 'anything'])

    assert result.exit_code == 1
    assert 'pipelines.p.step_function' in result.stderr


def test_an_unknown_name_reports_why_the_pipeline_tree_is_missing(invalid_config):
    # Every pipeline is a subcommand built from config, so a config that does not load takes
    # the whole tree with it and Click can only say the name is unknown.
    result = runner.invoke(app, ['p'])

    assert result.exit_code == 1
    assert 'does not match the expected schema' in result.stderr
    assert 'No such command' not in result.stderr


def test_an_unknown_name_stays_a_usage_error_when_the_config_loaded(monkeypatch):
    monkeypatch.setattr(dectl.main, 'CONFIG_ERROR', None)
    monkeypatch.setattr(dectl.main, 'cfg', DectlConfig.model_validate({'defaults': {'account_id': '1'}, 'pipelines': {}}))

    result = runner.invoke(app, ['definitely-not-a-command'])

    assert result.exit_code == 2
    assert 'No such command' in result.output


def test_the_bare_banner_carries_the_whole_diagnostic(invalid_config):
    # Bare `dectl` is where the missing pipelines are first noticed, so the banner carries the
    # reason rather than a pointer to the command that would print it.
    result = runner.invoke(app, [])

    assert 'pipelines.p.step_function: Extra inputs are not permitted' in result.stderr
    assert 'Usage: dectl' in result.stdout
