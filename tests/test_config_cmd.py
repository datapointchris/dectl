from typer.testing import CliRunner

from dectl.commands.config_cmd import config_app
from dectl.config import TEMPLATE_CONFIG

runner = CliRunner()


def test_path_prints_config_path(monkeypatch, tmp_path):
    config_path = tmp_path / 'config.yaml'
    monkeypatch.setattr('dectl.commands.config_cmd.CONFIG_PATH', config_path)

    result = runner.invoke(config_app, ['path'])

    assert result.exit_code == 0
    assert str(config_path) in result.stdout


def test_example_prints_every_option():
    # CliRunner output is not a TTY, so the plain-text branch runs and emits raw YAML.
    result = runner.invoke(config_app, ['example'])

    assert result.exit_code == 0
    for option in ('glue_jobs', 'lambdas', 'step_functions', 'buckets', 'monitor'):
        assert option in result.stdout


def test_validate_accepts_the_template(monkeypatch, tmp_path):
    config_path = tmp_path / 'config.yaml'
    config_path.write_text(TEMPLATE_CONFIG)
    monkeypatch.setattr('dectl.config.CONFIG_PATH', config_path)
    monkeypatch.setattr('dectl.commands.config_cmd.CONFIG_PATH', config_path)

    result = runner.invoke(config_app, ['validate'])

    assert result.exit_code == 0
    assert 'valid' in result.stdout


def test_validate_reports_missing_config(monkeypatch, tmp_path):
    config_path = tmp_path / 'nope.yaml'
    monkeypatch.setattr('dectl.commands.config_cmd.CONFIG_PATH', config_path)

    result = runner.invoke(config_app, ['validate'])

    assert result.exit_code == 1
    assert 'no config' in result.stdout


def test_validate_reports_unknown_key(monkeypatch, tmp_path):
    config_path = tmp_path / 'config.yaml'
    config_path.write_text('defaults:\n  account_id: "1"\npipelines:\n  p:\n    glue_jbs: {}\n')
    monkeypatch.setattr('dectl.config.CONFIG_PATH', config_path)
    monkeypatch.setattr('dectl.commands.config_cmd.CONFIG_PATH', config_path)

    result = runner.invoke(config_app, ['validate'])

    assert result.exit_code == 1
    assert 'glue_jbs' in result.stdout


def test_edit_seeds_config_then_opens_editor(monkeypatch, tmp_path):
    config_path = tmp_path / 'config.yaml'
    monkeypatch.setattr('dectl.config.CONFIG_DIR', tmp_path)
    monkeypatch.setattr('dectl.config.CONFIG_PATH', config_path)
    monkeypatch.setattr('dectl.commands.config_cmd.CONFIG_PATH', config_path)
    monkeypatch.setenv('VISUAL', 'cat')  # a real binary on PATH, so shutil.which resolves it
    monkeypatch.delenv('EDITOR', raising=False)

    invoked = []
    monkeypatch.setattr('dectl.commands.config_cmd.subprocess.run', lambda cmd, *a, **k: invoked.append(cmd))

    result = runner.invoke(config_app, ['edit'])

    assert result.exit_code == 0
    assert config_path.exists()  # seeded from the template
    assert invoked and invoked[0][-1] == str(config_path)


def test_edit_prefers_visual_over_editor(monkeypatch, tmp_path):
    config_path = tmp_path / 'config.yaml'
    config_path.write_text(TEMPLATE_CONFIG)
    monkeypatch.setattr('dectl.commands.config_cmd.CONFIG_PATH', config_path)
    monkeypatch.setenv('VISUAL', 'cat')
    monkeypatch.setenv('EDITOR', 'vi')

    invoked = []
    monkeypatch.setattr('dectl.commands.config_cmd.subprocess.run', lambda cmd, *a, **k: invoked.append(cmd))

    result = runner.invoke(config_app, ['edit'])

    assert result.exit_code == 0
    assert 'cat' in invoked[0][0]  # resolved from $VISUAL, not $EDITOR


def test_edit_errors_when_no_editor_configured(monkeypatch, tmp_path):
    config_path = tmp_path / 'config.yaml'
    config_path.write_text(TEMPLATE_CONFIG)
    monkeypatch.setattr('dectl.commands.config_cmd.CONFIG_PATH', config_path)
    monkeypatch.delenv('VISUAL', raising=False)
    monkeypatch.delenv('EDITOR', raising=False)

    result = runner.invoke(config_app, ['edit'])

    assert result.exit_code == 1
    assert 'no editor configured' in result.stdout
