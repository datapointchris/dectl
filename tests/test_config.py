import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest
from pydantic import ValidationError

from dectl.config import DectlConfig
from dectl.config import load_config


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
    type: glue
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


def test_validation_rejects_invalid_pipeline_type():
    raw = {
        'defaults': {'account_id': '111', 'region': 'us-east-1'},
        'pipelines': {'bad': {'type': 'unknown'}},
    }
    with pytest.raises(ValidationError):
        DectlConfig.model_validate(raw)


def test_glue_job_arguments_parsed_as_dict():
    raw = {
        'defaults': {'account_id': '111', 'region': 'us-east-1'},
        'pipelines': {
            'p': {
                'type': 'glue',
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


def test_defaults_have_sensible_fallbacks():
    raw = {
        'defaults': {'account_id': '111'},
        'pipelines': {},
    }
    config = DectlConfig.model_validate(raw)
    assert config.defaults.region == 'us-east-2'
    assert config.defaults.environment == 'dev'
    assert config.defaults.aws_profile == ''
