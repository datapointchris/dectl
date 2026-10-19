import json

import pytest

from dectl.config import ROOT_SITE
from dectl.config import DectlConfig
from dectl.config import GlueJobConfig
from dectl.config import declared_paths
from dectl.env import active_environment
from dectl.pipeline_view import pipeline_to_dict
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
    assert data['iceberg']['events'] == {'database': 'salesdata-prod-catalog', 'table': 'events', 'paths': {}}
    # Must be JSON-serializable with no rich markup leaking in.
    assert 'salesdata' in json.dumps(data)


def rooted_pipeline(**overrides) -> DectlConfig:
    pipeline = {
        'resolve_paths_from': '/srv/salesdata',
        'glue_jobs': {
            'copy': {'name': 'j', 'script_bucket': 'b-{env}', 'script_prefix': 'scripts/{env}', 'scripts': ['jobs/copy.py'], 'role': 'r'}
        },
        'lambdas': {'notifier': {'name': 'n', 'source_dir': 'modules/notifier/code'}},
    } | overrides
    return DectlConfig.model_validate({'defaults': {'account_id': '1'}, 'pipelines': {'salesdata': pipeline}})


def test_json_carries_the_resolved_path_of_every_declared_path():
    # Derived from `declared_paths` rather than asserted as two hand-written keys: giving
    # StepFunctionConfig a declared `definition_file` left the old assertion green while the
    # field was resolved and published nowhere, which is a completeness guard reporting complete
    # on a member it never looked at.
    pipeline = rooted_pipeline().pipelines['salesdata']

    data = pipeline_to_dict('salesdata', pipeline)

    # The sections that are not alias maps, named rather than skipped by shape. Skipping
    # anything that does not look like one makes a section published under the wrong shape
    # vanish from this walk, which is the guard reporting complete on a member it never saw.
    not_alias_keyed = {'pipeline', 'resolve_paths_from', 'monitor', 'jenkins'}
    assert not_alias_keyed <= set(data)

    published = set()
    for resource, aliases in data.items():
        if resource in not_alias_keyed:
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


# One valid, distinctive value per managed field, with whatever else the config needs to accept
# it — the worker pair is refused one half at a time. Looked up by the parametrize below rather
# than iterated, so a field added to either declaration arrives here as a KeyError naming it
# instead of as a field this guard silently never probed.
RENDERER_PROBES = {
    'max_capacity': {'max_capacity': 0.0625},
    'worker_type': {'worker_type': 'G.8X', 'number_of_workers': 7},
    'number_of_workers': {'worker_type': 'G.8X', 'number_of_workers': 7},
    'glue_version': {'glue_version': '4.2'},
    'python_version': {'python_version': '3.13'},
    'timeout_minutes': {'timeout_minutes': 4242},
    'max_retries': {'max_retries': 9},
    'max_concurrent_runs': {'max_concurrent_runs': 8},
    'execution_class': {'execution_class': 'FLEX'},
}


@pytest.mark.parametrize('field', sorted({**GlueJobConfig.DEFINITION_FIELDS, **GlueJobConfig.NESTED_DEFINITION_FIELDS}))
def test_every_managed_definition_field_reaches_json(field):
    """A field a deploy writes and `--json` omits is one no read verb can answer for."""
    configured = RENDERER_PROBES[field]
    config = DectlConfig.model_validate(
        {
            'defaults': {'account_id': '111', 'region': 'us-east-1'},
            'pipelines': {
                'salesdata': {
                    'glue_jobs': {
                        'j': {
                            'name': 'the-job',
                            'script_bucket': 'sales-scripts',
                            'scripts': ['main.py'],
                            'role': 'r',
                            **configured,
                        }
                    }
                }
            },
        }
    )
    pipeline = config.pipelines['salesdata']

    published = pipeline_to_dict('salesdata', pipeline)

    assert published['glue']['j']['managed'][field] == configured[field], f'{field} is managed and absent from --json'


def test_an_unmanaged_definition_field_is_absent_rather_than_null():
    # Absence is the whole meaning of an omitted key: the live value survives the deploy. A row
    # carrying null would say dectl writes a null, which is the one thing a reader must not
    # conclude from this block.
    config = DectlConfig.model_validate(
        {
            'defaults': {'account_id': '111', 'region': 'us-east-1'},
            'pipelines': {
                'salesdata': {'glue_jobs': {'j': {'name': 'n', 'script_bucket': 'sales-scripts', 'scripts': ['m.py'], 'role': 'r'}}}
            },
        }
    )

    published = pipeline_to_dict('salesdata', config.pipelines['salesdata'])

    assert published['glue']['j']['managed'] == {}
