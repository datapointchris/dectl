import json
from pathlib import Path

import pytest

from dectl.commands.config_cmd import config_app
from dectl.config import TEMPLATE_CONFIG
from dectl.env import set_active_environment
from tests.conftest import RefusalRunner
from tests.conftest import unwrapped

runner = RefusalRunner()


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

    result = runner.invoke(config_app, ['validate', '--json'])

    assert result.exit_code == 0
    assert json.loads(result.stdout)['outcome'] == 'valid'


def test_validate_reports_missing_config(monkeypatch, tmp_path):
    config_path = tmp_path / 'nope.yaml'
    monkeypatch.setattr('dectl.commands.config_cmd.CONFIG_PATH', config_path)

    result = runner.invoke(config_app, ['validate'])

    assert result.exit_code == 3
    assert 'no config' in result.stderr


def test_validate_reports_unknown_key(monkeypatch, tmp_path):
    config_path = tmp_path / 'config.yaml'
    config_path.write_text('defaults:\n  account_id: "1"\npipelines:\n  p:\n    glue_jbs: {}\n')
    monkeypatch.setattr('dectl.config.CONFIG_PATH', config_path)
    monkeypatch.setattr('dectl.commands.config_cmd.CONFIG_PATH', config_path)

    result = runner.invoke(config_app, ['validate'])

    assert result.exit_code == 3
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
                'path': str(root / 'modules' / 'code'),
                'fault': 'absent',
            }
        ],
    }


def test_validate_json_separates_a_clean_config_from_one_it_could_not_read(monkeypatch, tmp_path):
    root = tmp_path / 'salesdata'
    deployable(root / 'code')
    point_at(monkeypatch, tmp_path, config_naming(str(root)))

    clean = runner.invoke(config_app, ['validate', '--json'])
    point_at(monkeypatch, tmp_path, BAD_CONFIG)
    unreadable = runner.invoke(config_app, ['validate', '--json'])

    # Both carry an empty list. A caller that has dropped the exit status would otherwise be
    # told nothing was wrong with a config that was never read, so the status separates them too.
    assert clean.exit_code == 0
    assert json.loads(clean.stdout) == {'outcome': 'valid', 'unusable_values': []}
    assert unreadable.exit_code == 3
    assert json.loads(unreadable.stdout)['outcome'] == 'invalid_schema'


def test_validate_json_keeps_stdout_parseable_on_a_schema_failure(monkeypatch, tmp_path):
    # A caller piping into jq gets a parseable empty list and the reason on stderr, matching
    # what `show --json` does with the same config.
    point_at(monkeypatch, tmp_path, BAD_CONFIG)

    result = runner.invoke(config_app, ['validate', '--json'])

    assert result.exit_code == 3
    assert json.loads(result.stdout) == {'outcome': 'invalid_schema', 'unusable_values': []}
    assert 'pipelines.p.step_function' in result.stderr


@pytest.mark.parametrize(
    ('raw', 'outcome'),
    [
        ('pipelines: [oh: dear\n', 'invalid_yaml'),
        (BAD_CONFIG, 'invalid_schema'),
    ],
)
def test_validate_json_tells_the_two_load_failures_apart(monkeypatch, tmp_path, raw, outcome):
    # The human path prints "is not valid YAML" or "does not match the expected schema", and the
    # two have different fixes. The `--json` reader is the one who cannot see that sentence, so
    # folding both into one outcome left them with the stderr they are not reading.
    point_at(monkeypatch, tmp_path, raw)

    result = runner.invoke(config_app, ['validate', '--json'])

    assert result.exit_code == 3
    assert json.loads(result.stdout)['outcome'] == outcome


def test_validate_exits_the_same_way_for_every_fault_a_person_has_to_fix(monkeypatch, tmp_path):
    # Both need a person. The convention reserves 1 for drift a tool's own `apply` clears, and
    # dectl ships no `apply` — a config it could not read and one naming a directory that is not
    # there are both edits to the file. A scheduler holding that convention would read 1 as
    # self-clearing. Which kind it was is `outcome`, and `test_validate_json_tells_the_two_load
    # _failures_apart` is what pins that.
    root = tmp_path / 'salesdata'
    point_at(monkeypatch, tmp_path, config_naming(str(root)))
    unusable = runner.invoke(config_app, ['validate'])

    (tmp_path / 'config.yaml').unlink()
    absent = runner.invoke(config_app, ['validate'])

    assert unusable.exit_code == 3
    assert absent.exit_code == 3


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


def test_validate_rejects_a_present_root_that_is_missing_the_source(monkeypatch, tmp_path):
    # The readiness half. A root that exists and holds none of the source the pipeline names is
    # present and useless, and checking the directory alone would report it converged.
    root = tmp_path / 'salesdata'
    root.mkdir()
    raw = config_naming(str(root), source_dir='modules/code')
    point_at(monkeypatch, tmp_path, raw)

    result = runner.invoke(config_app, ['validate', '--json'])

    assert result.exit_code == 3
    assert faults_from(result) == [('lambda', 'source_dir', 'absent')]


def test_validate_reports_a_file_named_as_a_source_dir_as_a_file(monkeypatch, tmp_path):
    # Absent and present-with-the-wrong-type have opposite remedies — check the tree out, or fix
    # the config key — so they are separate values rather than one wording apart.
    root = tmp_path / 'salesdata'
    root.mkdir()
    (root / 'code').write_text('not a directory')
    raw = config_naming(str(root))
    point_at(monkeypatch, tmp_path, raw)

    result = runner.invoke(config_app, ['validate', '--json'])

    assert result.exit_code == 3
    assert faults_from(result) == [('lambda', 'source_dir', 'expected_directory')]


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


def test_env_is_substituted_inside_the_root_itself(monkeypatch, tmp_path):
    # Half-substituting the join is worse than not substituting at all: the resolved path is
    # neither what the config says nor what the environment asked for.
    root = tmp_path / 'dev' / 'salesdata'
    deployable(root / 'code')
    raw = config_naming(str(tmp_path / '{env}' / 'salesdata'))
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


def test_a_malformed_key_leaves_the_rest_of_the_cli_working(monkeypatch, tmp_path):
    # The whole reason the check is not a field validator. A schema failure sends main.py to
    # cfg = None and every pipeline command disappears, including the ones that would report it.
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

    result = runner.invoke(config_app, ['show', '--json'])

    assert result.exit_code == 0
    assert json.loads(result.stdout)[0]['pipeline'] == 'salesdata'


def test_validate_leaves_a_pipeline_without_a_declared_root_alone(monkeypatch, tmp_path):
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


SHOW_CONFIG = (
    'defaults:\n'
    '  account_id: "123456789012"\n'
    '  environment: dev\n'
    'pipelines:\n'
    '  p:\n'
    '    resolve_paths_from: ""\n'
    '    buckets:\n'
    '      raw: sales-{env}-raw\n'
)


def test_show_names_the_active_environment_not_the_configured_one(monkeypatch, tmp_path):
    # Every name below the row is substituted for the active environment, so the file's value
    # beside them reads `dev` four lines above `sales-prod-raw` — and `dectl env` says `prod`.
    point_at(monkeypatch, tmp_path, SHOW_CONFIG)
    set_active_environment('prod', '--env')

    result = runner.invoke(config_app, ['show'])

    assert result.exit_code == 0
    assert 'environment: prod (from --env)' in unwrapped(result.stdout)


def test_show_calls_an_empty_root_declared_because_the_json_door_does(monkeypatch, tmp_path):
    # Truthiness calls `resolve_paths_from: ""` unset while `pipeline_to_dict` publishes
    # `declared: true` for it. Two renderers disagreeing about one fact leaves the human one
    # telling the reader to add a key their file already has.
    point_at(monkeypatch, tmp_path, SHOW_CONFIG)

    result = runner.invoke(config_app, ['show'])

    assert result.exit_code == 0
    assert 'resolve_paths_from is unset' not in unwrapped(result.stdout)
