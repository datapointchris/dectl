import json

from dectl.config import DectlConfig
from dectl.env import active_environment
from dectl.pipeline_view import pipeline_to_dict
from dectl.pipeline_view import render_pipeline
from dectl.pipeline_view import resource_types


def make_pipeline() -> DectlConfig:
    return DectlConfig.model_validate(
        {
            'defaults': {'account_id': '123456789012'},
            'pipelines': {
                'salesdata': {
                    'glue_jobs': {
                        'source-copy': {
                            'name': 'salesdata-{env}-source-copy',
                            'script_bucket': 'salesdata-scripts',
                            'scripts': ['source_copy.py'],
                            'role': 'arn:aws:iam::123456789012:role/salesdata-{env}-glue',
                        }
                    },
                    'lambdas': {'notifier': {'name': 'salesdata-{env}-notifier', 'source_dir': 'code'}},
                    'step_functions': {'ingest': {'name': 'salesdata-{env}-ingest'}},
                    'buckets': {'raw': 'salesdata-{env}-raw'},
                }
            },
        }
    )


def test_resource_types_lists_configured_kinds_in_order():
    pipeline = make_pipeline().pipelines['salesdata']
    assert resource_types(pipeline) == ['glue', 'lambda', 'sfn', 's3']


def test_pipeline_to_dict_has_stable_shape_and_substitutes_env(monkeypatch):
    monkeypatch.setattr(active_environment, 'name', 'prod')
    pipeline = make_pipeline().pipelines['salesdata']

    data = pipeline_to_dict('salesdata', pipeline)

    assert data['pipeline'] == 'salesdata'
    assert data['glue']['source-copy']['name'] == 'salesdata-prod-source-copy'
    assert data['glue']['source-copy']['scripts'] == ['source_copy.py']
    assert data['lambda']['notifier']['name'] == 'salesdata-prod-notifier'
    assert data['sfn']['ingest']['name'] == 'salesdata-prod-ingest'
    assert data['s3']['raw']['bucket'] == 'salesdata-prod-raw'
    # Must be JSON-serializable with no rich markup leaking in.
    assert 'salesdata' in json.dumps(data)


def test_render_pipeline_prints_alias_to_name_lines(monkeypatch, capsys):
    monkeypatch.setattr(active_environment, 'name', 'dev')
    pipeline = make_pipeline().pipelines['salesdata']

    render_pipeline('salesdata', pipeline)

    out = capsys.readouterr().out
    assert 'glue/source-copy: salesdata-dev-source-copy' in out
    assert 's3/raw: salesdata-dev-raw' in out
