import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml
from pydantic import ValidationError

from dectl.config import TEMPLATE_CONFIG
from dectl.config import DectlConfig
from dectl.config import config_error_headline
from dectl.config import describe_config_error
from dectl.config import load_config


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
