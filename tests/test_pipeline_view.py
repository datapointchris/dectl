import json
import re
from pathlib import Path

from dectl.config import DectlConfig
from dectl.config import pipeline_root
from dectl.env import active_environment
from dectl.pipeline_view import aws_names_only
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


def test_json_carries_the_root_with_its_tilde_already_expanded():
    # The expansion happens between the file and the behaviour, so a reader who sees only the
    # written value cannot tell which directory a deploy would reach.
    config = DectlConfig.model_validate(
        {
            'defaults': {'account_id': '1'},
            'pipelines': {'salesdata': {'resolve_paths_from': '~/code/salesdata', 'buckets': {'raw': 'b'}}},
        }
    )

    data = pipeline_to_dict('salesdata', config.pipelines['salesdata'])

    assert data['resolve_paths_from'] == {'path': str(Path.home() / 'code' / 'salesdata'), 'declared': True}


def test_json_marks_an_unset_root_as_the_working_directory():
    # An unset root still resolves. Reporting the path with no flag beside it would read as a
    # declared value and hide that the resolution follows the shell.
    pipeline = make_pipeline().pipelines['salesdata']

    data = pipeline_to_dict('salesdata', pipeline)

    assert data['resolve_paths_from'] == {'path': str(Path.cwd()), 'declared': False}


def test_an_unset_root_is_reported_as_the_working_directory():
    # Asserted on the value rather than the printed line. A cwd long enough to wrap makes a
    # stdout assertion pass or fail on the width of whichever terminal ran it.
    pipeline = make_pipeline().pipelines['salesdata']

    assert pipeline_root(pipeline) == Path.cwd()
    assert pipeline.resolve_paths_from is None


def rooted_pipeline(**overrides) -> DectlConfig:
    pipeline = {
        'resolve_paths_from': '/srv/salesdata',
        'glue_jobs': {
            'copy': {'name': 'j', 'script_bucket': 'b-{env}', 'script_prefix': 'scripts/{env}', 'scripts': ['jobs/copy.py'], 'role': 'r'}
        },
        'lambdas': {'notifier': {'name': 'n', 'source_dir': 'modules/notifier/code'}},
    } | overrides
    return DectlConfig.model_validate({'defaults': {'account_id': '1'}, 'pipelines': {'salesdata': pipeline}})


def test_a_glue_job_with_no_scripts_renders_through_both_doors():
    # scripts: [] passes the schema, so declared_paths yields no row for the job and an indexed
    # lookup raises. The human door survived it and the machine door did not, on a config main
    # rendered fine.
    config = DectlConfig.model_validate(
        {
            'defaults': {'account_id': '1'},
            'pipelines': {
                'p': {'resolve_paths_from': '/srv/x', 'glue_jobs': {'j': {'name': 'n', 'script_bucket': 'b', 'scripts': [], 'role': 'r'}}}
            },
        }
    )
    pipeline = config.pipelines['p']

    data = pipeline_to_dict('p', pipeline)
    render_pipeline('p', pipeline)

    assert data['glue']['j']['script_paths'] == []


def test_an_alias_with_a_space_survives_every_consumer():
    # The alias is a field on DeclaredPath, not something split back out of the human label.
    # A label-parsing consumer recovers 'weird' from 'glue/weird alias script' and loses the
    # rest, so this is the input that separates structure from a split.
    config = DectlConfig.model_validate(
        {
            'defaults': {'account_id': '1'},
            'pipelines': {
                'salesdata': {
                    'resolve_paths_from': '/srv/salesdata',
                    'lambdas': {'weird alias': {'name': 'n', 'source_dir': 'code'}},
                }
            },
        }
    )
    pipeline = config.pipelines['salesdata']

    data = pipeline_to_dict('salesdata', pipeline)

    assert data['lambda']['weird alias']['source_path'] == '/srv/salesdata/code'
    assert aws_names_only(pipeline)['lambdas']['weird alias'].get('source_dir') is None


def test_json_carries_the_resolved_path_of_every_declared_path():
    # The reader is asking which file a deploy sends. The config string does not answer that,
    # and the repo row alone does not either.
    pipeline = rooted_pipeline().pipelines['salesdata']

    data = pipeline_to_dict('salesdata', pipeline)

    assert data['glue']['copy']['script_paths'] == ['/srv/salesdata/jobs/copy.py']
    assert data['lambda']['notifier']['source_path'] == '/srv/salesdata/modules/notifier/code'


def test_json_substitutes_the_script_prefix_like_every_other_name(monkeypatch):
    # The prefix is half the destination. Leaving it literal beside a substituted bucket names
    # an S3 location no deploy writes to.
    monkeypatch.setattr(active_environment, 'name', 'prod')
    pipeline = rooted_pipeline().pipelines['salesdata']

    data = pipeline_to_dict('salesdata', pipeline)

    assert data['glue']['copy']['script_prefix'] == 'scripts/prod'
    assert data['glue']['copy']['script_bucket'] == 'b-prod'


def test_render_prints_the_resolved_path_of_every_declared_path(capsys):
    pipeline = rooted_pipeline().pipelines['salesdata']

    render_pipeline('salesdata', pipeline)

    out = re.sub(r'\s+', ' ', capsys.readouterr().out)
    assert '/srv/salesdata/jobs/copy.py' in out
    assert 'source_dir: /srv/salesdata/modules/notifier/code' in out


def test_a_source_dir_token_does_not_silence_the_no_effect_warning(monkeypatch, capsys):
    # The guard asks whether --env changed an AWS name. Every local path field has to be out of
    # the dump it reads, not just `repo`, or one of them satisfies the check and silences it.
    monkeypatch.setattr(active_environment, 'name', 'prod')
    monkeypatch.setattr(active_environment, 'source', '--env')
    monkeypatch.setattr(active_environment, 'warned_about_missing_placeholder', False)
    config = DectlConfig.model_validate(
        {
            'defaults': {'account_id': '1'},
            'pipelines': {
                'salesdata': {
                    'resolve_paths_from': '/srv/salesdata',
                    'lambdas': {'notifier': {'name': 'hardcoded-dev', 'source_dir': 'modules/{env}/code'}},
                }
            },
        }
    )

    pipeline_to_dict('salesdata', config.pipelines['salesdata'])

    assert 'changed nothing' in capsys.readouterr().err


def test_a_glue_script_token_does_not_silence_the_no_effect_warning(monkeypatch, capsys):
    monkeypatch.setattr(active_environment, 'name', 'prod')
    monkeypatch.setattr(active_environment, 'source', '--env')
    monkeypatch.setattr(active_environment, 'warned_about_missing_placeholder', False)
    config = DectlConfig.model_validate(
        {
            'defaults': {'account_id': '1'},
            'pipelines': {
                'salesdata': {
                    'resolve_paths_from': '/srv/salesdata',
                    'glue_jobs': {'copy': {'name': 'hardcoded-dev', 'script_bucket': 'b', 'scripts': ['jobs/{env}/c.py'], 'role': 'r'}},
                }
            },
        }
    )

    pipeline_to_dict('salesdata', config.pipelines['salesdata'])

    assert 'changed nothing' in capsys.readouterr().err


def test_a_root_token_does_not_silence_the_no_effect_warning(monkeypatch, capsys):
    # The guard asks whether --env changed an AWS name. A {env} in a local path satisfies
    # contains_env_placeholder without naming anything in AWS, so it must not reach the dump.
    monkeypatch.setattr(active_environment, 'name', 'prod')
    monkeypatch.setattr(active_environment, 'source', '--env')
    monkeypatch.setattr(active_environment, 'warned_about_missing_placeholder', False)
    config = DectlConfig.model_validate(
        {
            'defaults': {'account_id': '1'},
            'pipelines': {'salesdata': {'resolve_paths_from': '~/code/{env}/salesdata', 'buckets': {'raw': 'hardcoded-bucket'}}},
        }
    )

    pipeline_to_dict('salesdata', config.pipelines['salesdata'])

    assert 'changed nothing' in capsys.readouterr().err
