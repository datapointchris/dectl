import json

import pytest
import typer
from pydantic import ValidationError

import dectl.main
from dectl.commands.config_cmd import config_app
from dectl.config import DectlConfig
from dectl.config import Defaults
from dectl.main import REFERENCE_GLOBAL
from dectl.main import app
from tests.conftest import RefusalRunner

runner = RefusalRunner()


def test_reference_prints_the_grammar():
    result = runner.invoke(app, ['reference'])

    assert result.exit_code == 0
    assert 'RESOURCE ALIAS VERB' in result.stdout
    assert 'Instance verbs' in result.stdout
    assert 'Set / pipeline verbs' in result.stdout
    # Option-syntax brackets must survive verbatim, not be eaten as rich markup tags.
    assert '[--follow]' in result.stdout


def row_entries(row: str) -> list[str]:
    """One reference row's entries, as `verb [flags]` strings.

    Rows are ` · `-separated, and the `config` row leads with its own label. Split rather than
    searched as one string: the `config` row contains the substring `path`, so a top-level
    `path` command missing from the row that should name it satisfied a search of the joined
    text. A guard one row can satisfy on another row's behalf is not a guard."""
    label, _, rest = row.partition('  ')
    entries = (rest or label).split('·')
    return [entry.strip() for entry in entries if entry.strip()]


GLOBAL_ENTRIES = row_entries(REFERENCE_GLOBAL[0])
CONFIG_ENTRIES = row_entries(REFERENCE_GLOBAL[1])


def names_in(entries: list[str]) -> set[str]:
    return {entry.split(' ')[0] for entry in entries}


def test_the_reference_names_every_static_global_command():
    # `reference` exists because --help shows almost nothing on a fresh machine, so a command
    # missing from it is invisible to the reader it was written for. Derived from the tree
    # rather than listed here: a command added to either app has to appear without this test
    # being edited.
    tree = typer.main.get_command(app)
    # A leaf, not a group: the pipeline sub-apps are assembled from config and are exactly what
    # `reference` exists to be independent of.
    static = {name for name, command in tree.commands.items() if not hasattr(command, 'commands')}

    assert static
    assert static - names_in(GLOBAL_ENTRIES) == set()


def test_the_reference_names_every_config_verb_and_its_json_flag():
    # The flags too, not only the verbs. `validate --json` was added to the README and to the
    # command while the reference kept the flagless spelling, which is the half of the surface a
    # reader on a fresh machine actually sees.
    config_tree = typer.main.get_command(config_app)
    verbs = set(config_tree.commands)
    with_json = {name for name, command in config_tree.commands.items() if any(param.name == 'as_json' for param in command.params)}

    assert verbs and with_json
    assert verbs - names_in(CONFIG_ENTRIES) == set()
    assert {f'{verb} [--json]' for verb in with_json} - set(CONFIG_ENTRIES) == set()


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


def test_version_is_one_line_naming_the_tool(monkeypatch):
    monkeypatch.setattr(dectl.main.importlib.metadata, 'version', lambda _: '9.9.9')
    monkeypatch.setattr(dectl.main, 'installed_commit', lambda: None)

    result = runner.invoke(app, ['--version'])

    assert result.exit_code == 0
    assert result.stdout.splitlines() == ['dectl 9.9.9']


def test_version_carries_the_commit_when_installed_from_a_ref(monkeypatch):
    monkeypatch.setattr(dectl.main.importlib.metadata, 'version', lambda _: '9.9.9')
    monkeypatch.setattr(dectl.main, 'installed_commit', lambda: 'abcdef1234567890')

    result = runner.invoke(app, ['--version'])

    assert result.stdout.splitlines() == ['dectl 9.9.9 @ abcdef12']


def test_version_answers_a_config_that_does_not_load(invalid_config, monkeypatch):
    # The reason the option is eager. A tool that cannot say which build it is because its
    # config is unreadable is the case a version is most often asked for.
    monkeypatch.setattr(dectl.main.importlib.metadata, 'version', lambda _: '9.9.9')
    monkeypatch.setattr(dectl.main, 'installed_commit', lambda: None)

    result = runner.invoke(app, ['--version'])

    assert result.exit_code == 0
    assert result.stdout.splitlines() == ['dectl 9.9.9']
    assert 'does not match the expected schema' not in result.stderr


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
    # Not 'Usage: dectl': rich styles the two words differently, so an escape sequence sits
    # between them wherever color is on, and the phrase is only contiguous without it.
    assert 'Usage:' in result.stdout


def test_env_json_carries_every_default_the_human_view_shows(monkeypatch):
    """`config show` prints four resolved defaults and no machine door carried any of them.

    `aws_profile` is the one that decides which AWS account a deploy reaches. Derived from
    `Defaults.model_fields`, so a field added to the model reaches this document without an
    edit — the same derivation the human rows use, which is what keeps the two level."""
    config = DectlConfig.model_validate({'defaults': {'account_id': '123456789012', 'aws_profile': 'data-eng'}, 'pipelines': {}})
    monkeypatch.setattr(dectl.main, 'CONFIG_ERROR', None)
    monkeypatch.setattr(dectl.main, 'cfg', config)

    result = runner.invoke(dectl.main.app, ['--env', 'prod', 'env', '--json'])

    assert result.exit_code == 0
    published = json.loads(result.stdout)
    assert published == {
        'environment': 'prod',
        'source': '--env',
        'account_id': '123456789012',
        'region': 'us-east-2',
        'aws_profile': 'data-eng',
    }
    assert set(Defaults.model_fields) - {'environment'} <= set(published)
