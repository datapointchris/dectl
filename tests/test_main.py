from typer.testing import CliRunner

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
