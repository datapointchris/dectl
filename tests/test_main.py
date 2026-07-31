from typer.testing import CliRunner

import dectl.main
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
