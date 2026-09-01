import os
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml
from pydantic import ValidationError

from dectl.config import CONFIG_LOAD_ERRORS
from dectl.config import TEMPLATE_CONFIG
from dectl.config import DectlConfig
from dectl.config import PipelineConfig
from dectl.config import config_error_headline
from dectl.config import describe_config_error
from dectl.config import load_config
from dectl.config import pipeline_root
from dectl.config import resolve_in_repo
from dectl.env import set_active_environment


def test_template_config_is_valid():
    # The example config that `config init` writes and `config example` prints must always
    # round-trip through the models, so the example can never drift out of validity.
    config = DectlConfig.model_validate(yaml.safe_load(TEMPLATE_CONFIG))
    assert 'example-pipeline' in config.pipelines


def test_validation_rejects_unknown_pipeline_key():
    raw = {
        'defaults': {'account_id': '111'},
        'pipelines': {'p': {'step_function': {}}},  # typo: should be step_functions
    }
    with pytest.raises(ValidationError):
        DectlConfig.model_validate(raw)


def test_validation_rejects_unknown_defaults_key():
    raw = {
        'defaults': {'account_id': '111', 'regon': 'us-east-1'},  # typo: should be region
        'pipelines': {},
    }
    with pytest.raises(ValidationError):
        DectlConfig.model_validate(raw)


def test_load_config_returns_none_when_file_missing():
    with patch('dectl.config.CONFIG_PATH', Path('/nonexistent/config.yaml')):
        assert load_config() is None


def test_load_config_parses_valid_yaml():
    content = """
defaults:
  account_id: "111111111111"
  region: us-west-2
pipelines:
  foo:
    glue_jobs:
      bar:
        name: foo-bar-job
        script_bucket: foo-bucket
        scripts: [main.py]
        role: "arn:aws:iam::111111111111:role/foo-role"
"""
    with tempfile.NamedTemporaryFile(mode='w', suffix='.yaml', delete=False) as f:
        f.write(content)
        f.flush()
        with patch('dectl.config.CONFIG_PATH', Path(f.name)):
            config = load_config()
            assert config is not None
            assert config.defaults.region == 'us-west-2'
            assert 'foo' in config.pipelines


def test_validation_rejects_missing_account_id():
    raw = {'defaults': {'region': 'us-east-1'}, 'pipelines': {}}
    with pytest.raises(ValidationError):
        DectlConfig.model_validate(raw)


def test_validation_rejects_invalid_glue_job():
    raw = {
        'defaults': {'account_id': '111', 'region': 'us-east-1'},
        'pipelines': {'bad': {'glue_jobs': {'j': {'scripts': ['s.py']}}}},
    }
    with pytest.raises(ValidationError):
        DectlConfig.model_validate(raw)


def test_glue_job_arguments_parsed_as_dict():
    raw = {
        'defaults': {'account_id': '111', 'region': 'us-east-1'},
        'pipelines': {
            'p': {
                'glue_jobs': {
                    'j': {
                        'name': 'j',
                        'script_bucket': 'b',
                        'scripts': ['s.py'],
                        'role': 'r',
                        'arguments': {'FOO': 'bar', 'NUM': '4'},
                    }
                },
            }
        },
    }
    config = DectlConfig.model_validate(raw)
    job = config.pipelines['p'].glue_jobs['j']
    assert job.arguments == {'FOO': 'bar', 'NUM': '4'}


def test_lambda_live_alias_field_parsed():
    raw = {
        'defaults': {'account_id': '111'},
        'pipelines': {'p': {'lambdas': {'fn': {'name': 'n', 'source_dir': 'd', 'live_alias': 'live'}}}},
    }
    config = DectlConfig.model_validate(raw)
    assert config.pipelines['p'].lambdas['fn'].live_alias == 'live'


def test_lambda_rejects_renamed_alias_key():
    # The field was renamed alias -> live_alias; extra='forbid' turns the stale key into a loud
    # error rather than a silent drop, so a not-yet-migrated config fails validate instead of
    # silently never moving the alias on deploy --publish.
    raw = {
        'defaults': {'account_id': '111'},
        'pipelines': {'p': {'lambdas': {'fn': {'name': 'n', 'source_dir': 'd', 'alias': 'live'}}}},
    }
    with pytest.raises(ValidationError):
        DectlConfig.model_validate(raw)


def test_buckets_parsed_as_shortname_mapping():
    raw = {
        'defaults': {'account_id': '111', 'region': 'us-east-1'},
        'pipelines': {'p': {'buckets': {'raw': 'my-raw-bucket', 'curated': 'my-curated-bucket'}}},
    }
    config = DectlConfig.model_validate(raw)
    assert config.pipelines['p'].buckets == {'raw': 'my-raw-bucket', 'curated': 'my-curated-bucket'}


def test_buckets_default_to_empty_when_omitted():
    raw = {
        'defaults': {'account_id': '111'},
        'pipelines': {'p': {}},
    }
    config = DectlConfig.model_validate(raw)
    assert config.pipelines['p'].buckets == {}


def test_step_functions_parsed_with_optional_log_group():
    raw = {
        'defaults': {'account_id': '111'},
        'pipelines': {
            'p': {
                'step_functions': {
                    'flow': {'name': 'my-flow', 'log_group': '/aws/vendedlogs/states/my-flow'},
                    'bare': {'name': 'bare-flow'},
                }
            }
        },
    }
    config = DectlConfig.model_validate(raw)
    step_functions = config.pipelines['p'].step_functions
    assert step_functions['flow'].log_group == '/aws/vendedlogs/states/my-flow'
    assert step_functions['bare'].log_group == ''


def test_monitor_selection_parsed():
    raw = {
        'defaults': {'account_id': '111'},
        'pipelines': {'p': {'monitor': {'lambdas': ['a', 'b'], 'step_functions': ['flow']}}},
    }
    config = DectlConfig.model_validate(raw)
    monitor = config.pipelines['p'].monitor
    assert monitor.lambdas == ['a', 'b']
    assert monitor.step_functions == ['flow']


def test_monitor_defaults_to_empty():
    raw = {'defaults': {'account_id': '111'}, 'pipelines': {'p': {}}}
    config = DectlConfig.model_validate(raw)
    assert config.pipelines['p'].monitor.lambdas == []
    assert config.pipelines['p'].monitor.step_functions == []


def test_defaults_have_sensible_fallbacks():
    raw = {
        'defaults': {'account_id': '111'},
        'pipelines': {},
    }
    config = DectlConfig.model_validate(raw)
    assert config.defaults.region == 'us-east-2'
    assert config.defaults.environment == 'dev'
    assert config.defaults.aws_profile == ''


def validation_error_for(raw) -> ValidationError:
    """The real exception a bad config produces, since the renderer walks `.errors()`."""
    with pytest.raises(ValidationError) as caught:
        DectlConfig.model_validate(raw)
    return caught.value


def test_describe_names_the_location_and_the_rejected_value():
    # The rejected value is what identifies the offending key: `step_function` is a plausible
    # spelling, and the location alone leaves the reader hunting for which block held it.
    exc = validation_error_for(
        {
            'defaults': {'account_id': '111'},
            'pipelines': {'p': {'step_function': {'flow': {'name': 'sm'}}}},
        }
    )

    lines = describe_config_error(exc)

    assert lines[0] == '  pipelines.p.step_function: Extra inputs are not permitted'
    assert lines[1] == "    got: {'flow': {'name': 'sm'}}"


def test_describe_truncates_a_large_rejected_value():
    # A `missing` error's input is the whole parent object, so a real pipeline block would
    # bury the message it is meant to illustrate.
    exc = validation_error_for({'pipelines': {'p': {'buckets': {f'alias-{n}': f'bucket-{n}' for n in range(40)}}}})

    lines = describe_config_error(exc)

    assert lines[0] == '  defaults: Field required'
    assert lines[1].endswith('…')
    assert len(lines[1]) < 200


def test_describe_reports_every_problem_not_only_the_first():
    exc = validation_error_for({'defaults': {'account_id': '111', 'acct': 'x'}, 'pipelines': {'p': {'glue_jbs': {}}}})

    lines = describe_config_error(exc)

    assert '  defaults.acct: Extra inputs are not permitted' in lines
    assert '  pipelines.p.glue_jbs: Extra inputs are not permitted' in lines


def test_a_yaml_syntax_error_reports_through_the_same_renderer():
    # Both failures are caught together everywhere, so both have to render.
    with pytest.raises(yaml.YAMLError) as caught:
        yaml.safe_load('defaults:\n  account_id: "1"\n pipelines: {}\n')

    lines = describe_config_error(caught.value)

    assert lines
    assert all(line.startswith('  ') for line in lines)


def test_the_headline_distinguishes_the_two_failures():
    schema_error = validation_error_for({'pipelines': {}})
    with pytest.raises(yaml.YAMLError) as caught:
        yaml.safe_load('a:\n b: 1\n  c: 2\n')

    assert 'does not match the expected schema' in config_error_headline(schema_error)
    assert 'is not valid YAML' in config_error_headline(caught.value)


def config_path_in_a_fresh_interpreter(env: dict[str, str]) -> str:
    """The path is resolved at import, so the environment has to be set before the process starts."""
    result = subprocess.run(
        [sys.executable, '-c', 'from dectl.config import CONFIG_PATH; print(CONFIG_PATH)'],
        capture_output=True,
        text=True,
        env=env,
        check=True,
    )
    return result.stdout.strip()


def test_config_path_follows_xdg_config_home(tmp_path):
    resolved = config_path_in_a_fresh_interpreter({**os.environ, 'XDG_CONFIG_HOME': str(tmp_path)})

    assert resolved == str(tmp_path / 'dectl' / 'config.yaml')


def test_config_path_falls_back_to_dot_config_when_xdg_is_unset():
    env = {key: value for key, value in os.environ.items() if key != 'XDG_CONFIG_HOME'}

    resolved = config_path_in_a_fresh_interpreter(env)

    assert resolved == str(Path.home() / '.config' / 'dectl' / 'config.yaml')


def pipeline_with_repo(repo) -> PipelineConfig:
    raw = {'lambdas': {'fn': {'name': 'n', 'source_dir': 'code'}}}
    if repo is not None:
        raw['repo'] = repo
    return PipelineConfig.model_validate(raw)


def test_repo_anchors_a_relative_path(tmp_path):
    pipeline = pipeline_with_repo(str(tmp_path))

    assert resolve_in_repo(pipeline, 'modules/lambda/code') == tmp_path / 'modules' / 'lambda' / 'code'


def test_repo_expands_a_leading_tilde():
    pipeline = pipeline_with_repo('~/code/salesdata')

    assert resolve_in_repo(pipeline, 'code') == Path.home() / 'code' / 'salesdata' / 'code'


def test_paths_resolve_against_the_working_directory_without_a_repo():
    # The behaviour a config that names no repo depends on: dectl is run from the checkout and
    # relative paths mean what they would mean to any other command in that shell.
    pipeline = pipeline_with_repo(None)

    assert resolve_in_repo(pipeline, 'code') == Path.cwd() / 'code'


def test_an_absolute_configured_path_ignores_the_repo(tmp_path):
    # Mixed absolute and repo-relative entries are legal, so an absolute one has to survive
    # being joined onto a repo that is nowhere near it.
    pipeline = pipeline_with_repo(str(tmp_path / 'repo'))

    assert resolve_in_repo(pipeline, '/srv/shared/handler') == Path('/srv/shared/handler')


def test_pipeline_root_is_the_working_directory_without_a_repo():
    assert pipeline_root(pipeline_with_repo(None)) == Path.cwd()


def test_relative_repo_is_rejected():
    # A relative repo would resolve against the working directory, which is the dependency the
    # key exists to remove — so it fails validation rather than half-working.
    with pytest.raises(ValidationError):
        pipeline_with_repo('../salesdata')


def test_repo_is_absent_by_default():
    assert pipeline_with_repo(None).repo is None


def test_a_repo_naming_no_real_user_fails_as_a_schema_error():
    # `~code/x` is a missing slash, the likeliest typo in a path key. Path.expanduser() raises
    # RuntimeError for it, pydantic does not wrap that, and CONFIG_LOAD_ERRORS does not catch
    # it — so uncaught it escapes main.py's import guard and takes down `config edit` too.
    with pytest.raises(ValidationError):
        pipeline_with_repo('~code/salesdata')


def test_a_source_dir_naming_no_real_user_fails_as_a_schema_error():
    with pytest.raises(ValidationError):
        PipelineConfig.model_validate({'lambdas': {'fn': {'name': 'n', 'source_dir': '~code/x'}}})


def test_every_config_load_failure_is_one_the_callers_catch():
    # The property behind both tests above. Every caller catches CONFIG_LOAD_ERRORS, so a load
    # failure outside that tuple is one no command recovers from.
    for bad in ('~code/salesdata', '../relative'):
        with pytest.raises(CONFIG_LOAD_ERRORS):
            pipeline_with_repo(bad)


def test_the_env_token_is_substituted_into_the_repo():
    # A token left literal in the repo while source_dir substitutes gives a half-resolved join,
    # which is neither what the config says nor what --env asked for.
    set_active_environment('prod', '--env')
    try:
        pipeline = pipeline_with_repo('/srv/{env}/salesdata')
        assert resolve_in_repo(pipeline, 'modules/{env}/code') == Path('/srv/prod/salesdata/modules/prod/code')
    finally:
        set_active_environment('dev', 'default')


def test_a_repo_whose_root_is_a_token_is_still_rejected_as_relative():
    # The token is stripped rather than substituted for the rootedness test, because --env is
    # parsed after the config loads.
    with pytest.raises(ValidationError):
        pipeline_with_repo('{env}/salesdata')
