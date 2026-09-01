import json
from pathlib import Path

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
                    'iceberg_tables': {'events': {'database': 'salesdata-{env}-catalog', 'table': 'events'}},
                }
            },
        }
    )


def test_resource_types_lists_configured_kinds_in_order():
    pipeline = make_pipeline().pipelines['salesdata']
    assert resource_types(pipeline) == ['glue', 'lambda', 'sfn', 's3', 'iceberg']


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
    assert data['iceberg']['events'] == {'database': 'salesdata-prod-catalog', 'table': 'events'}
    # Must be JSON-serializable with no rich markup leaking in.
    assert 'salesdata' in json.dumps(data)


def test_render_pipeline_prints_alias_to_name_lines(monkeypatch, capsys):
    monkeypatch.setattr(active_environment, 'name', 'dev')
    pipeline = make_pipeline().pipelines['salesdata']

    render_pipeline('salesdata', pipeline)

    out = capsys.readouterr().out
    assert 'glue/source-copy: salesdata-dev-source-copy' in out
    assert 's3/raw: salesdata-dev-raw' in out
    assert 'iceberg/events: salesdata-dev-catalog.events' in out


def test_json_carries_the_repo_with_its_tilde_already_expanded():
    # The expansion happens between the file and the behaviour, so a reader who sees only the
    # written value cannot tell which directory a deploy would reach.
    config = DectlConfig.model_validate(
        {
            'defaults': {'account_id': '1'},
            'pipelines': {'salesdata': {'repo': '~/code/salesdata', 'buckets': {'raw': 'b'}}},
        }
    )

    data = pipeline_to_dict('salesdata', config.pipelines['salesdata'])

    assert data['repo'] == {'path': str(Path.home() / 'code' / 'salesdata'), 'declared': True}


def test_json_marks_an_undeclared_repo_as_the_working_directory():
    # An undeclared repo still resolves. Reporting the path with no flag beside it would read
    # as a declared value and hide that the resolution follows the shell.
    pipeline = make_pipeline().pipelines['salesdata']

    data = pipeline_to_dict('salesdata', pipeline)

    assert data['repo'] == {'path': str(Path.cwd()), 'declared': False}


def test_render_names_the_working_directory_when_no_repo_is_set(monkeypatch, capsys):
    # A tmp-length cwd plus the suffix runs past a default-width console, and rich would wrap
    # the assertion's needle across two lines.
    monkeypatch.setenv('COLUMNS', '200')
    monkeypatch.setattr(active_environment, 'name', 'dev')
    pipeline = make_pipeline().pipelines['salesdata']

    render_pipeline('salesdata', pipeline)

    out = capsys.readouterr().out
    assert f'repo: {Path.cwd()}' in out
    assert 'no repo set' in out
