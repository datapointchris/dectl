import json
import re
from pathlib import Path

import pytest

from dectl.commands.glue import script_uri
from dectl.config import ROOT_SITE
from dectl.config import DectlConfig
from dectl.config import GlueJobConfig
from dectl.config import IcebergTableConfig
from dectl.config import LambdaConfig
from dectl.config import StepFunctionConfig
from dectl.config import declared_paths
from dectl.config import pipeline_root
from dectl.env import active_environment
from dectl.env import render_env_model
from dectl.pipeline_view import aws_names_only
from dectl.pipeline_view import pipeline_to_dict
from dectl.pipeline_view import render_pipeline
from dectl.pipeline_view import resolved_paths
from dectl.pipeline_view import resource_types
from dectl.values import PathKind


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
    assert data['iceberg']['events'] == {'database': 'salesdata-prod-catalog', 'table': 'events', 'paths': {}}
    # Must be JSON-serializable with no rich markup leaking in.
    assert 'salesdata' in json.dumps(data)


def test_render_pipeline_prints_alias_to_name_lines(monkeypatch, capsys):
    # Every alias and every resolved name reaches the page. Asserted as tokens rather than as
    # `alias: name` phrases, because rich soft-wraps between the two at a width the consumer
    # owns — and normalising the wrap to keep a phrase assertion alive is a width pin by
    # another name, which fails at exactly the widths a width pin would have covered.
    monkeypatch.setattr(active_environment, 'name', 'dev')
    pipeline = make_pipeline().pipelines['salesdata']

    render_pipeline('salesdata', pipeline)

    printed = set(capsys.readouterr().out.split())
    assert {'glue/source-copy:', 'salesdata-dev-source-copy'} <= printed
    assert {'s3/raw:', 'salesdata-dev-raw'} <= printed
    assert {'iceberg/events:', 'salesdata-dev-catalog.events'} <= printed


def test_json_carries_the_root_with_its_tilde_already_expanded():
    # The expansion happens between the file and the behaviour, so a reader who sees only the
    # written value cannot tell which directory a deploy would reach.
    config = DectlConfig.model_validate(
        {
            'defaults': {'account_id': '1'},
            'pipelines': {'salesdata': {'resolve_paths_from': '~/code/salesdata', 'buckets': {'raw': 'sales-raw'}}},
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
                'p': {
                    'resolve_paths_from': '/srv/x',
                    'glue_jobs': {'j': {'name': 'n', 'script_bucket': 'sales-scripts', 'scripts': [], 'role': 'r'}},
                }
            },
        }
    )
    pipeline = config.pipelines['p']

    data = pipeline_to_dict('p', pipeline)
    render_pipeline('p', pipeline)

    assert data['glue']['j']['paths'] == {}


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

    assert data['lambda']['weird alias']['paths'] == {'source_dir': ['/srv/salesdata/code']}
    assert aws_names_only(pipeline)['lambdas']['weird alias'].get('source_dir') is None


def test_json_carries_the_resolved_path_of_every_declared_path():
    # Derived from `declared_paths` rather than asserted as two hand-written keys: giving
    # StepFunctionConfig a declared `definition_file` left the old assertion green while the
    # field was resolved and published nowhere, which is a completeness guard reporting complete
    # on a member it never looked at.
    pipeline = rooted_pipeline().pipelines['salesdata']

    data = pipeline_to_dict('salesdata', pipeline)

    published = set()
    for resource, aliases in data.items():
        if resource == 'resolve_paths_from' or not isinstance(aliases, dict):
            continue
        for alias, body in aliases.items():
            published.update((resource, alias, field) for field in body.get('paths', {}))

    expected = set()
    for declared in declared_paths(pipeline):
        if declared.site != ROOT_SITE:
            expected.add((declared.site.resource, declared.site.alias, declared.site.field))

    assert published == expected

    assert data['glue']['copy']['paths'] == {'scripts': ['/srv/salesdata/jobs/copy.py']}
    assert data['lambda']['notifier']['paths'] == {'source_dir': ['/srv/salesdata/modules/notifier/code']}


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
    # the dump it reads, not just the root, or one of them satisfies the check and silences it.
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

    assert active_environment.warned_about_missing_placeholder


def test_a_glue_script_token_is_an_aws_name_and_silences_the_warning(monkeypatch, capsys):
    # A glue script is a local path and the second operand of its S3 key, so `--env` changing it
    # changes the object S3 holds and the ScriptLocation naming it. Warning that `--env changed
    # nothing` there prints a sentence that is false about the very field it is looking at.
    monkeypatch.setattr(active_environment, 'name', 'prod')
    monkeypatch.setattr(active_environment, 'source', '--env')
    monkeypatch.setattr(active_environment, 'warned_about_missing_placeholder', False)
    config = DectlConfig.model_validate(
        {
            'defaults': {'account_id': '1'},
            'pipelines': {
                'salesdata': {
                    'resolve_paths_from': '/srv/salesdata',
                    'glue_jobs': {
                        'copy': {'name': 'hardcoded-dev', 'script_bucket': 'sales-scripts', 'scripts': ['jobs/{env}/c.py'], 'role': 'r'}
                    },
                }
            },
        }
    )

    pipeline_to_dict('salesdata', config.pipelines['salesdata'])

    assert not active_environment.warned_about_missing_placeholder


@pytest.mark.parametrize(
    ('resource', 'warns'),
    [
        (LambdaConfig(name='hardcoded-dev', source_dir='modules/{env}/code'), True),
        (GlueJobConfig(name='hardcoded-dev', script_bucket='sales-scripts', scripts=['jobs/{env}/c.py'], role='r'), False),
    ],
    ids=['lambda source_dir is local only', 'glue script is also a key operand'],
)
def test_the_deploy_door_asks_the_same_question_as_the_read_door(monkeypatch, capsys, resource, warns):
    # The read door and the write door ask the same question of the same resource, so they get
    # the same answer. `render_env_model` is what every deploy verb resolves through, and it
    # passed the whole dump — so the guard fired on `list` and went quiet on the deploy that
    # acts on the wrong environment, which is the case it exists for.
    #
    # A lambda `source_dir` is zipped and uploaded as bytes, so nothing in AWS carries its name
    # and a token there answers the guard's question falsely. A glue script's name reaches S3.
    monkeypatch.setattr(active_environment, 'name', 'prod')
    monkeypatch.setattr(active_environment, 'source', '--env')
    monkeypatch.setattr(active_environment, 'warned_about_missing_placeholder', False)

    render_env_model(resource)

    assert active_environment.warned_about_missing_placeholder == warns


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

    assert active_environment.warned_about_missing_placeholder


def test_every_resolved_path_reaches_both_renderers():
    # `resolved_paths` is driven by the declaration and both renderers look rows up in it by
    # resource and alias. Their sections carry per-kind AWS fields, so they are written out
    # rather than generated, and a resource kind without one is resolved and then displayed by
    # neither. That is a missing row and nothing else goes red for it, so it goes red here.
    config = DectlConfig.model_validate(
        {
            'defaults': {'account_id': '1'},
            'pipelines': {
                'salesdata': {
                    'resolve_paths_from': '/srv/salesdata',
                    'glue_jobs': {'copy': {'name': 'j', 'script_bucket': 'b', 'scripts': ['c.py'], 'role': 'r'}},
                    'lambdas': {'fn': {'name': 'n', 'source_dir': 'code'}},
                    'step_functions': {'flow': {'name': 'm'}},
                    'iceberg_tables': {'events': {'database': 'd', 'table': 't'}},
                    'buckets': {'raw': 'sales-raw'},
                }
            },
        }
    )
    pipeline = config.pipelines['salesdata']
    published = pipeline_to_dict('salesdata', pipeline)

    for (resource, alias), fields in resolved_paths(pipeline).items():
        section = published.get(resource)
        assert section is not None, f'{resource} has resolved paths and no section in --json'
        assert alias in section, f'{resource}/{alias} has resolved paths and no row in --json'
        assert section[alias]['paths'] == fields


@pytest.mark.parametrize(
    ('kind', 'owner', 'field'),
    [
        ('glue', GlueJobConfig, 'scripts'),
        ('lambda', LambdaConfig, 'source_dir'),
        ('sfn', StepFunctionConfig, 'name'),
        ('iceberg', IcebergTableConfig, 'table'),
    ],
)
def test_every_resolved_path_reaches_the_human_renderer(capsys, monkeypatch, kind, owner, field):
    # The other renderer, asserted separately because it is a separate hand-written site:
    # `render_pipeline` calls `print_paths` once per resource kind, so a kind whose call is
    # missing resolves and prints nothing while `--json` still carries it.
    #
    # A declaration is put on each kind rather than relying on the ones that exist. `sfn` and
    # `iceberg` declare no path field today, so a test reading only what they already resolve
    # loops over nothing and passes with their `print_paths` call deleted — which is the state
    # this test was written to end.
    monkeypatch.setattr(owner, 'PATH_FIELDS', {field: PathKind.FILE})
    config = DectlConfig.model_validate(
        {
            'defaults': {'account_id': '1'},
            'pipelines': {
                'salesdata': {
                    'resolve_paths_from': '/srv/salesdata',
                    'glue_jobs': {'copy': {'name': 'j', 'script_bucket': 'b', 'scripts': ['c.py'], 'role': 'r'}},
                    'lambdas': {'fn': {'name': 'n', 'source_dir': 'code'}},
                    'step_functions': {'flow': {'name': 'm'}},
                    'iceberg_tables': {'events': {'database': 'd', 'table': 't'}},
                }
            },
        }
    )
    pipeline = config.pipelines['salesdata']
    resolved = resolved_paths(pipeline)
    assert any(resource == kind for resource, _ in resolved), f'{kind} declares a path and resolves none'

    render_pipeline('salesdata', pipeline)
    printed = capsys.readouterr().out

    for (resource, alias), fields in resolved.items():
        for name, paths in fields.items():
            for path in paths:
                assert path in printed, f'{resource}/{alias} {name} resolves to {path} and is printed nowhere'


def test_a_bare_field_member_replaces_its_collection_in_the_env_guard_dump():
    # `monitor` is held as a bare field, so it has no alias to be filed under. Filing it under
    # the empty one leaves the unfiltered original beside the filtered copy, and the env-effect
    # guard reads the original — a `{env}` in a local path goes on silencing the warning.
    config = DectlConfig.model_validate(
        {
            'defaults': {'account_id': '1'},
            'pipelines': {'salesdata': {'monitor': {'lambdas': ['a'], 'step_functions': ['b']}}},
        }
    )

    dumped = aws_names_only(config.pipelines['salesdata'])['monitor']

    assert '' not in dumped
    assert dumped == {'lambdas': ['a'], 'step_functions': ['b']}


def test_the_deploy_and_both_renderers_name_one_destination():
    # Four sites name where a script lands, and they read one builder. Dropping the prefix's
    # substitution inside it left the suite green while the deploy wrote `s3://b/p-{env}/c.py`
    # and `config show --json` reported `s3://b/p-dev/c.py` — one file under two names, which
    # is the failure the whole key surface exists to prevent.
    config = DectlConfig.model_validate(
        {
            'defaults': {'account_id': '1'},
            'pipelines': {
                'salesdata': {
                    'glue_jobs': {
                        'copy': {
                            'name': 'j',
                            'script_bucket': 'b-{env}',
                            'script_prefix': 'p-{env}',
                            'scripts': ['c-{env}.py'],
                            'role': 'r',
                        }
                    }
                }
            },
        }
    )
    job = config.pipelines['salesdata'].glue_jobs['copy']

    published = pipeline_to_dict('salesdata', config.pipelines['salesdata'])['glue']['copy']

    assert script_uri(job, 'c-{env}.py') == 's3://b-dev/p-dev/c-dev.py'
    assert published['script_uris'] == ['s3://b-dev/p-dev/c-dev.py']
    assert '{env}' not in published['script_uris'][0]
