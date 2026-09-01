import json

import yaml
from typer.testing import CliRunner

from dectl.commands.config_cmd import config_app
from dectl.config import TEMPLATE_CONFIG
from dectl.config import DectlConfig
from dectl.config import missing_declared_paths

runner = CliRunner()


def test_show_json_emits_pipeline_shape(monkeypatch, tmp_path):
    config_path = tmp_path / 'config.yaml'
    config_path.write_text(TEMPLATE_CONFIG)
    monkeypatch.setattr('dectl.config.CONFIG_PATH', config_path)
    monkeypatch.setattr('dectl.commands.config_cmd.CONFIG_PATH', config_path)

    result = runner.invoke(config_app, ['show', '--json'])

    assert result.exit_code == 0
    data = json.loads(result.stdout)
    assert data[0]['pipeline'] == 'example-pipeline'
    assert 'source-copy' in data[0]['glue']


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
    assert 'no config' in result.stderr


def test_validate_reports_unknown_key(monkeypatch, tmp_path):
    config_path = tmp_path / 'config.yaml'
    config_path.write_text('defaults:\n  account_id: "1"\npipelines:\n  p:\n    glue_jbs: {}\n')
    monkeypatch.setattr('dectl.config.CONFIG_PATH', config_path)
    monkeypatch.setattr('dectl.commands.config_cmd.CONFIG_PATH', config_path)

    result = runner.invoke(config_app, ['validate'])

    assert result.exit_code == 1
    # The detail is a refusal, not an answer, so it goes to stderr like every other one.
    assert 'glue_jbs' in result.stderr
    assert result.stdout == ''


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
    assert 'no editor configured' in result.stderr


BAD_CONFIG = 'defaults:\n  account_id: "1"\npipelines:\n  p:\n    step_function: {}\n'


def point_at(monkeypatch, tmp_path, contents: str | None):
    """Both modules hold their own CONFIG_PATH reference, so both have to be redirected.

    Patching only the command module leaves load_config() reading the real config on the
    machine running the tests, which passes or fails on what happens to be there.

    COLUMNS widens the rich console for the duration: a tmp_path is far longer than a real
    config path, and the headline would otherwise wrap mid-assertion.
    """
    monkeypatch.setenv('COLUMNS', '200')
    config_path = tmp_path / 'config.yaml'
    if contents is not None:
        config_path.write_text(contents)
    monkeypatch.setattr('dectl.config.CONFIG_PATH', config_path)
    monkeypatch.setattr('dectl.commands.config_cmd.CONFIG_PATH', config_path)
    return config_path


def test_show_reports_an_invalid_config_instead_of_raising(monkeypatch, tmp_path):
    # load_config() raises on an invalid config, so an unguarded call here surfaces as a
    # pydantic traceback — the most detailed thing dectl can print and the least usable.
    point_at(monkeypatch, tmp_path, BAD_CONFIG)

    result = runner.invoke(config_app, ['show'])

    assert result.exit_code == 1
    assert result.exception is None or isinstance(result.exception, SystemExit)
    assert 'does not match the expected schema' in result.stderr
    assert 'pipelines.p.step_function' in result.stderr
    assert 'Traceback' not in result.stderr


def test_show_json_keeps_stdout_empty_when_the_config_is_invalid(monkeypatch, tmp_path):
    # A caller piping this into jq gets a clean empty stdout and the reason on stderr.
    point_at(monkeypatch, tmp_path, BAD_CONFIG)

    result = runner.invoke(config_app, ['show', '--json'])

    assert result.exit_code == 1
    assert result.stdout == ''
    assert 'pipelines.p.step_function' in result.stderr


def test_show_and_validate_report_an_invalid_config_identically(monkeypatch, tmp_path):
    # One renderer, so the answer does not depend on which command the reader reached for.
    point_at(monkeypatch, tmp_path, BAD_CONFIG)

    shown = runner.invoke(config_app, ['show'])
    validated = runner.invoke(config_app, ['validate'])

    assert shown.stderr == validated.stderr


def test_show_still_names_a_missing_config_as_missing(monkeypatch, tmp_path):
    point_at(monkeypatch, tmp_path, None)

    result = runner.invoke(config_app, ['show'])

    assert result.exit_code == 1
    assert 'no config found' in result.stderr
    assert 'config init' in result.stderr


def config_naming(repo: str, source_dir: str = 'code') -> str:
    return (
        'defaults:\n'
        '  account_id: "1"\n'
        'pipelines:\n'
        '  salesdata:\n'
        f'    repo: {repo}\n'
        '    lambdas:\n'
        '      notifier:\n'
        '        name: n\n'
        f'        source_dir: {source_dir}\n'
    )


def test_validate_accepts_a_repo_whose_paths_are_all_present(monkeypatch, tmp_path):
    repo = tmp_path / 'salesdata'
    (repo / 'code').mkdir(parents=True)
    point_at(monkeypatch, tmp_path, config_naming(str(repo)))

    result = runner.invoke(config_app, ['validate'])

    assert result.exit_code == 0
    assert 'valid' in result.stdout


def config_from(raw: str) -> DectlConfig:
    return DectlConfig.model_validate(yaml.safe_load(raw))


def test_validate_rejects_a_repo_that_is_not_on_this_machine(monkeypatch, tmp_path):
    # The value, not the rendered line: rich wraps a long path at the running terminal's width,
    # and a path assertion against wrapped stderr passes or fails on the pane it ran in.
    raw = config_naming(str(tmp_path / 'absent'))
    point_at(monkeypatch, tmp_path, raw)

    result = runner.invoke(config_app, ['validate'])

    assert result.exit_code == 1
    assert [(p.label, p.path, p.fault) for p in missing_declared_paths(config_from(raw))] == [('repo', tmp_path / 'absent', 'not found')]


def test_validate_rejects_a_present_repo_that_is_missing_the_source(monkeypatch, tmp_path):
    # The readiness half. A repo that exists and holds none of the source the pipeline names is
    # present and useless, and checking the directory alone would report it converged.
    repo = tmp_path / 'salesdata'
    repo.mkdir()
    raw = config_naming(str(repo), source_dir='modules/code')
    point_at(monkeypatch, tmp_path, raw)

    result = runner.invoke(config_app, ['validate'])

    assert result.exit_code == 1
    assert [(p.label, p.path, p.fault) for p in missing_declared_paths(config_from(raw))] == [
        ('lambda/notifier source_dir', repo / 'modules' / 'code', 'not found')
    ]


def test_validate_reports_a_file_named_as_a_source_dir_as_a_file(monkeypatch, tmp_path):
    # Absent and present-with-the-wrong-type have opposite remedies, so the wording separates
    # them: check the tree out, or fix the config key.
    repo = tmp_path / 'salesdata'
    repo.mkdir()
    (repo / 'code').write_text('not a directory')
    raw = config_naming(str(repo))
    point_at(monkeypatch, tmp_path, raw)

    result = runner.invoke(config_app, ['validate'])

    assert result.exit_code == 1
    assert [p.fault for p in missing_declared_paths(config_from(raw))] == ['is a file, not a directory']


def test_validate_checks_glue_scripts_and_not_only_lambda_sources(monkeypatch, tmp_path):
    # Deleting the glue half of the walk left the suite green, so the glue half was unproved.
    repo = tmp_path / 'salesdata'
    (repo / 'code').mkdir(parents=True)
    raw = (
        'defaults:\n'
        '  account_id: "1"\n'
        'pipelines:\n'
        '  salesdata:\n'
        f'    repo: {repo}\n'
        '    glue_jobs:\n'
        '      copy:\n'
        '        name: n\n'
        '        script_bucket: b\n'
        '        role: r\n'
        '        scripts:\n'
        '          - jobs/copy.py\n'
        '    lambdas:\n'
        '      notifier:\n'
        '        name: n\n'
        '        source_dir: code\n'
    )
    point_at(monkeypatch, tmp_path, raw)

    result = runner.invoke(config_app, ['validate'])

    assert result.exit_code == 1
    assert [(p.label, p.path) for p in missing_declared_paths(config_from(raw))] == [('glue/copy script', repo / 'jobs' / 'copy.py')]


def test_env_is_substituted_before_a_declared_path_is_checked(monkeypatch, tmp_path):
    # The deploys resolve these through render_env_model, so a validator reading the raw model
    # would fail a config whose deploy works.
    repo = tmp_path / 'salesdata'
    (repo / 'modules' / 'dev' / 'code').mkdir(parents=True)
    raw = config_naming(str(repo), source_dir='modules/{env}/code')
    point_at(monkeypatch, tmp_path, raw)

    result = runner.invoke(config_app, ['validate'])

    assert result.exit_code == 0
    assert missing_declared_paths(config_from(raw)) == []


def test_env_is_substituted_inside_the_repo_itself(monkeypatch, tmp_path):
    # Half-substituting the join is worse than not substituting at all: the resolved path is
    # neither what the config says nor what the environment asked for.
    repo = tmp_path / 'dev' / 'salesdata'
    (repo / 'code').mkdir(parents=True)
    raw = config_naming(str(tmp_path / '{env}' / 'salesdata'))
    point_at(monkeypatch, tmp_path, raw)

    result = runner.invoke(config_app, ['validate'])

    assert result.exit_code == 0
    assert missing_declared_paths(config_from(raw)) == []


def test_validate_leaves_a_pipeline_without_a_repo_alone(monkeypatch, tmp_path):
    # Its paths resolve against wherever dectl is run from, so their absence here says nothing.
    # Checking them anyway would fail validate on a config that is correct.
    raw = (
        'defaults:\n'
        '  account_id: "1"\n'
        'pipelines:\n'
        '  salesdata:\n'
        '    lambdas:\n'
        '      notifier:\n'
        '        name: n\n'
        '        source_dir: nowhere/at/all\n'
    )
    point_at(monkeypatch, tmp_path, raw)

    result = runner.invoke(config_app, ['validate'])

    assert result.exit_code == 0
