import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml
from pydantic import ValidationError

from dectl.config import TEMPLATE_CONFIG
from dectl.config import DectlConfig
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
