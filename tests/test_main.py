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


class _Completed:
    """What `subprocess.run` hands back, in the two fields github_token reads."""

    def __init__(self, stdout, returncode=0):
        self.stdout = stdout
        self.returncode = returncode


def test_the_gh_credential_is_offered_lazily(monkeypatch):
    """Eager resolution would put a `gh` spawn in front of every dectl command."""
    spawns = []
    monkeypatch.setattr(dectl.main.shutil, 'which', lambda _name: '/usr/bin/gh')
    monkeypatch.setattr(dectl.main.subprocess, 'run', lambda *args, **kwargs: spawns.append(args) or _Completed('  tok\n'))

    assert dectl.main.UPDATE_CONFIG.token_func is dectl.main.github_token
    assert spawns == [], 'building the config must not have asked gh for anything'
    assert dectl.main.github_token() == 'tok'


def test_a_missing_gh_leaves_the_lookup_unauthenticated(monkeypatch):
    """Anonymous is 60 requests an hour per IP, which is worse than a token and
    better than an update command that cannot run without one."""
    monkeypatch.setattr(dectl.main.shutil, 'which', lambda _name: None)

    assert dectl.main.github_token() == ''


def test_an_unauthenticated_gh_leaves_the_lookup_unauthenticated(monkeypatch):
    """`gh auth token` exits non-zero when nobody has logged in on this machine."""
    monkeypatch.setattr(dectl.main.shutil, 'which', lambda _name: '/usr/bin/gh')
    monkeypatch.setattr(dectl.main.subprocess, 'run', lambda *args, **kwargs: _Completed('', returncode=1))

    assert dectl.main.github_token() == ''
