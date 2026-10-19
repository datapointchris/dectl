import json
import re
from pathlib import Path

import pytest

from dectl.commands.config_cmd import config_app
from dectl.config import TEMPLATE_CONFIG
from dectl.config import init_config
from dectl.config import load_config
from tests.conftest import RefusalRunner
from tests.conftest import write_config_files

runner = RefusalRunner()


def test_path_prints_config_path(monkeypatch, tmp_path):
    config_path = tmp_path / 'config.yaml'
    monkeypatch.setattr('dectl.commands.config_cmd.CONFIG_PATH', config_path)

    result = runner.invoke(config_app, ['path'])

    assert result.exit_code == 0
    assert str(config_path) in result.stdout


def test_each_file_the_example_prints_loads_when_copied_to_its_path(monkeypatch, tmp_path):
    # CliRunner output is not a TTY, which selects the piped branch.
    result = runner.invoke(config_app, ['example'])
    parts = re.split(r'^# ((?:pipelines/)?[\w-]+\.yaml)\n', result.stdout, flags=re.MULTILINE)
    (tmp_path / 'pipelines').mkdir()
    for path, text in zip(parts[1::2], parts[2::2], strict=True):
        (tmp_path / path).write_text(text)
    monkeypatch.setattr('dectl.config.CONFIG_PATH', tmp_path / 'config.yaml')

    config = load_config()

    assert config is not None
    assert set(config.pipelines) == {'example-pipeline', 'buckets-only'}


def test_validate_accepts_the_template(monkeypatch, tmp_path):
    config_path = tmp_path / 'config.yaml'
    monkeypatch.setattr('dectl.config.CONFIG_PATH', config_path)
    monkeypatch.setattr('dectl.commands.config_cmd.CONFIG_PATH', config_path)
    init_config()

    result = runner.invoke(config_app, ['validate', '--json'])

    assert result.exit_code == 0
    assert json.loads(result.stdout)['outcome'] == 'valid'


def test_edit_opens_config_yaml_bare_and_a_pipeline_file_by_name(monkeypatch, tmp_path):
    config_path = tmp_path / 'config.yaml'
    monkeypatch.setattr('dectl.config.CONFIG_PATH', config_path)
    monkeypatch.setattr('dectl.commands.config_cmd.CONFIG_PATH', config_path)
    monkeypatch.setenv('VISUAL', 'cat')  # a real binary on PATH, so shutil.which resolves it
    monkeypatch.delenv('EDITOR', raising=False)

    invoked = []
    monkeypatch.setattr('dectl.commands.config_cmd.subprocess.run', lambda cmd, *a, **k: invoked.append(cmd[-1]))

    bare = runner.invoke(config_app, ['edit'])
    named = runner.invoke(config_app, ['edit', 'orders'])
    # The file's name rather than the pipeline's would write orders.yaml.yaml, a second pipeline.
    by_file_name = runner.invoke(config_app, ['edit', 'orders.yaml'])

    assert (bare.exit_code, named.exit_code, by_file_name.exit_code) == (0, 0, 1)
    pipeline_path = tmp_path / 'pipelines' / 'orders.yaml'
    assert invoked == [str(config_path), str(pipeline_path)]
    # Both were created, and what was created loads: the template, and the stub under its name.
    config = load_config()
    assert config is not None
    assert 'orders' in config.pipelines


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
        write_config_files(tmp_path, contents)
    monkeypatch.setattr('dectl.config.CONFIG_PATH', config_path)
    monkeypatch.setattr('dectl.commands.config_cmd.CONFIG_PATH', config_path)
    return config_path


def test_show_json_keeps_stdout_empty_when_the_config_is_invalid(monkeypatch, tmp_path):
    # A caller piping this into jq gets a clean empty stdout and the reason on stderr.
    point_at(monkeypatch, tmp_path, BAD_CONFIG)

    result = runner.invoke(config_app, ['show', '--json'])

    assert result.exit_code == 1
    assert result.stdout == ''
    assert 'pipelines/p.yaml step_function' in result.stderr


def test_validate_json_emits_the_unusable_paths_as_objects(monkeypatch, tmp_path):
    root = tmp_path / 'salesdata'
    root.mkdir()
    point_at(monkeypatch, tmp_path, config_naming(str(root), source_dir='modules/code'))

    result = runner.invoke(config_app, ['validate', '--json'])

    assert result.exit_code == 3
    assert json.loads(result.stdout) == {
        'outcome': 'unusable_values',
        'unusable_values': [
            {
                'pipeline': 'salesdata',
                'resource': 'lambda',
                'alias': 'notifier',
                'field': 'source_dir',
                'configured': 'modules/code',
                'written': None,
                'variables': [],
                'path': str(root / 'modules' / 'code'),
                'fault': 'absent',
            }
        ],
    }


@pytest.mark.parametrize(
    ('raw', 'outcome', 'where'),
    [
        ('pipelines: [oh: dear\n', 'invalid_yaml', 'in "{path}", line'),
        (BAD_CONFIG, 'invalid_schema', 'pipelines/p.yaml step_function'),
    ],
)
def test_validate_json_tells_the_two_load_failures_apart(monkeypatch, tmp_path, raw, outcome, where):
    # The human path prints "is not valid YAML" or "does not match the expected schema", and the
    # two have different fixes. The `--json` reader is the one who cannot see that sentence, so
    # folding both into one outcome left them with the stderr they are not reading.
    config_path = point_at(monkeypatch, tmp_path, raw)

    result = runner.invoke(config_app, ['validate', '--json'])

    assert result.exit_code == 3
    assert json.loads(result.stdout)['outcome'] == outcome
    assert where.format(path=config_path) in result.stderr


def config_naming(root: str, source_dir: str = 'code') -> str:
    return (
        'defaults:\n'
        '  account_id: "1"\n'
        'pipelines:\n'
        '  salesdata:\n'
        f'    resolve_paths_from: {root}\n'
        '    lambdas:\n'
        '      notifier:\n'
        '        name: n\n'
        f'        source_dir: {source_dir}\n'
    )


def deployable(path: Path) -> Path:
    """A source directory holding something, which is what a valid one has to be.

    An empty directory is a fault of its own: it zips to a 22-byte archive that
    `update_function_code` accepts, replacing the function's code with nothing."""
    path.mkdir(parents=True)
    (path / 'handler.py').write_text('def handler(event, context): pass')
    return path


def faults_from(result) -> list[tuple[str, str, str]]:
    """The (resource, field, fault) triples `validate --json` reported.

    Read off the published document rather than off the sentence stderr rendered. `ConfigFault`
    exists so the wording can change without breaking a consumer, and a test matching the
    wording is a consumer of it — it goes red on a reworded message and stays green on a wrong
    fault, which is the pair of failures backwards."""
    return [(p['resource'], p['field'], p['fault']) for p in json.loads(result.stdout)['unusable_values']]


def test_validate_accepts_a_root_whose_paths_are_all_present(monkeypatch, tmp_path):
    root = tmp_path / 'salesdata'
    deployable(root / 'code')
    point_at(monkeypatch, tmp_path, config_naming(str(root)))

    result = runner.invoke(config_app, ['validate', '--json'])

    assert result.exit_code == 0
    assert json.loads(result.stdout)['outcome'] == 'valid'


def test_validate_rejects_a_root_that_is_not_on_this_machine(monkeypatch, tmp_path):
    point_at(monkeypatch, tmp_path, config_naming(str(tmp_path / 'absent')))

    result = runner.invoke(config_app, ['validate', '--json'])

    assert result.exit_code == 3
    assert faults_from(result) == [('pipeline', 'resolve_paths_from', 'absent')]


def test_validate_checks_glue_scripts_and_not_only_lambda_sources(monkeypatch, tmp_path):
    # Both halves of the walk are checked. Deleting either one has to turn this red.
    root = tmp_path / 'salesdata'
    deployable(root / 'code')
    raw = (
        'defaults:\n'
        '  account_id: "1"\n'
        'pipelines:\n'
        '  salesdata:\n'
        f'    resolve_paths_from: {root}\n'
        '    glue_jobs:\n'
        '      copy:\n'
        '        name: n\n'
        '        script_bucket: sales-scripts\n'
        '        role: r\n'
        '        scripts:\n'
        '          - jobs/copy.py\n'
        '    lambdas:\n'
        '      notifier:\n'
        '        name: n\n'
        '        source_dir: code\n'
    )
    point_at(monkeypatch, tmp_path, raw)

    result = runner.invoke(config_app, ['validate', '--json'])

    assert result.exit_code == 3
    assert faults_from(result) == [('glue', 'scripts', 'absent')]
    assert json.loads(result.stdout)['unusable_values'][0]['path'] == str(root / 'jobs' / 'copy.py')


def test_env_is_substituted_before_a_declared_path_is_checked(monkeypatch, tmp_path):
    # The deploys resolve these through render_env_model, so a validator reading the raw model
    # would fail a config whose deploy works.
    root = tmp_path / 'salesdata'
    deployable(root / 'modules' / 'dev' / 'code')
    raw = config_naming(str(root), source_dir='modules/{env}/code')
    point_at(monkeypatch, tmp_path, raw)

    result = runner.invoke(config_app, ['validate', '--json'])

    assert result.exit_code == 0
    assert json.loads(result.stdout)['outcome'] == 'valid'


def test_validate_checks_key_shape_even_without_a_declared_root(monkeypatch, tmp_path):
    # How a key is written is a property of the config, answerable on any machine. Only whether
    # a file is present needs a root to say where to look.
    raw = (
        'defaults:\n'
        '  account_id: "1"\n'
        'pipelines:\n'
        '  salesdata:\n'
        '    glue_jobs:\n'
        '      copy:\n'
        '        name: n\n'
        '        script_bucket: sales-scripts\n'
        '        role: r\n'
        '        scripts:\n'
        '          - /srv/shared/handler.py\n'
    )
    point_at(monkeypatch, tmp_path, raw)

    result = runner.invoke(config_app, ['validate', '--json'])

    assert result.exit_code == 3
    assert [p['fault'] for p in json.loads(result.stdout)['unusable_values']] == ['key_escapes_root']
