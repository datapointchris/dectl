import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import get_args
from unittest.mock import patch

import pytest
import typer
import yaml
from pydantic import BaseModel
from pydantic import ValidationError
from pydantic import field_validator

from dectl.config import CONFIG_LOAD_ERRORS
from dectl.config import TEMPLATE_CONFIG
from dectl.config import DectlConfig
from dectl.config import GlueJobConfig
from dectl.config import IcebergTableConfig
from dectl.config import LambdaConfig
from dectl.config import MonitorConfig
from dectl.config import PipelineConfig
from dectl.config import ResourceModel
from dectl.config import StepFunctionConfig
from dectl.config import StrictModel
from dectl.config import config_error_headline
from dectl.config import declared_names
from dectl.config import declared_paths
from dectl.config import declared_values
from dectl.config import describe_config_error
from dectl.config import load_config
from dectl.config import pipeline_root
from dectl.config import pipeline_value_faults
from dectl.config import resolve_from_root
from dectl.env import render_env_model
from dectl.env import set_active_environment
from dectl.values import FAULT_WORDING
from dectl.values import SPELLING_FAULTS
from dectl.values import ConfigFault
from dectl.values import PathKind
from dectl.values import deployable_files
from dectl.values import path_fault


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
                        'script_bucket': 'sales-scripts',
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


# Every string-valued config field that dectl deliberately does not check, with the reason. A
# new field lands in the assertion below rather than in here by default, so adding one is a
# decision about which of the three declarations it belongs in — or an entry here saying why it
# belongs in none. A name-based heuristic cannot do that job: it read six words plus a literal
# `scripts`, so `asl`, `handler`, `template`, `manifest` and `entrypoint` were all invisible.
UNCHECKED_FIELDS = {
    (GlueJobConfig, 'name'): 'a Glue job name; GetJob is what says whether it exists',
    (GlueJobConfig, 'role'): 'an IAM role ARN; UpdateJob validates it',
    (GlueJobConfig, 'arguments'): 'Glue job arguments, passed through; Glue validates them',
    (GlueJobConfig, 'connections'): 'Glue connection names; UpdateJob validates them',
    (LambdaConfig, 'name'): 'a Lambda function name; the API is what says whether it exists',
    (LambdaConfig, 'live_alias'): 'a Lambda alias; update_alias validates it',
    (StepFunctionConfig, 'name'): 'a state machine name; the API is what says whether it exists',
    (StepFunctionConfig, 'log_group'): 'a CloudWatch log group; monitor reports it unset',
    (IcebergTableConfig, 'database'): 'a Glue Data Catalog database; GetTable reports its absence',
    (IcebergTableConfig, 'table'): 'a Glue Data Catalog table; GetTable reports its absence',
    (MonitorConfig, 'lambdas'): 'aliases into this pipeline; build_monitor_sources warns on a dangling one',
    (MonitorConfig, 'step_functions'): 'aliases into this pipeline; build_monitor_sources warns on a dangling one',
}

# What a declared field's value may be: one string, or a list or mapping of them. Anything else
# is not a value dectl resolves.
STRING_ANNOTATIONS = (str, str | None, list[str], list[str] | None, dict[str, str])


def string_fields(model: type[BaseModel]) -> set[str]:
    return {name for name, field in model.model_fields.items() if field.annotation in STRING_ANNOTATIONS}


def resource_models() -> set[type[ResourceModel]]:
    """Every `ResourceModel` a pipeline can hold, walked the way `resource_members` walks one.

    Both a bare annotation and a `dict[str, Model]` one, because the code reaches both. Walking
    only `get_args` left a declaration on `monitor` — a bare `MonitorConfig` field — inert with
    the whole suite green, which is the guard reporting complete on a member it cannot see."""
    found: set[type[ResourceModel]] = {PipelineConfig}
    for field in PipelineConfig.model_fields.values():
        candidates = [field.annotation, *get_args(field.annotation)]
        found |= {arg for arg in candidates if isinstance(arg, type) and issubclass(arg, ResourceModel)}
    return found


def test_the_model_walk_reaches_every_resource_kind():
    # The walk above is only as good as the evidence that it reaches something, and an empty one
    # satisfies every assertion below. Named outright, so a new resource kind is a decision here
    # too rather than something that silently joins or silently does not.
    assert {model.RESOURCE for model in resource_models()} == {'pipeline', 'glue', 'lambda', 'sfn', 'iceberg', 'monitor'}


def test_every_string_field_is_classified():
    # The invariant the design rests on: three declarations say what dectl checks about a field,
    # and everything that checks, resolves or excludes a value reads one of them. A field in
    # none of them and not named above is checked by nothing, and that reads as success — the
    # config validates and the env-effect guard goes quiet.
    unclassified = set()
    for model in resource_models():
        declared = set(model.PATH_FIELDS) | model.KEY_FIELDS | model.BUCKET_FIELDS
        for field in string_fields(model) - declared:
            unclassified.add((model, field))

    assert unclassified - set(UNCHECKED_FIELDS) == set()


def test_every_declared_path_field_reaches_declared_paths():
    # The declaration is only half of it: a field declared in PATH_FIELDS and never enumerated
    # is checked by nothing just as surely as an undeclared one.
    pipeline = PipelineConfig.model_validate(
        {
            'resolve_paths_from': '/srv/salesdata',
            'glue_jobs': {'j': {'name': 'n', 'script_bucket': 'sales-scripts', 'scripts': ['x.py'], 'role': 'r'}},
            'lambdas': {'fn': {'name': 'n', 'source_dir': 'code'}},
            'step_functions': {'flow': {'name': 'm'}},
            'buckets': {'raw': 'sales-raw'},
            'iceberg_tables': {'events': {'database': 'd', 'table': 't'}},
        }
    )
    declared_on_models = set()
    for model in resource_models():
        declared_on_models.update((model.RESOURCE, field) for field in model.PATH_FIELDS)

    enumerated = {(path.site.resource, path.site.field) for path in declared_paths(pipeline)}

    assert declared_on_models - enumerated == set()


@pytest.mark.parametrize(
    ('owner', 'raw', 'resource', 'alias'),
    [
        # Held in a dict, keyed by alias — the shape the design was written around.
        (LambdaConfig, {'lambdas': {'fn': {'name': 'n', 'source_dir': 'code'}}}, 'lambda', 'fn'),
        # Held as a bare field, with no alias. Three consumers walked their own way to a set
        # that excluded this, and each reported no fault rather than going red.
        (MonitorConfig, {'monitor': {'lambdas': ['a']}}, 'monitor', ''),
        # The pipeline declares too, and is not in `resource_members` at all.
        (PipelineConfig, {'resolve_paths_from': '/srv/x'}, 'pipeline', ''),
    ],
)
def test_every_declaration_consumer_sees_every_member_shape(monkeypatch, owner, raw, resource, alias):
    # `declaring_members` is the one walk, and this is what keeps it one. A consumer that
    # traverses for itself reaches a narrower set, and the failure is silent in every direction:
    # a value nothing enumerates is a value nothing checks, and no check means no fault.
    field = next(iter(owner.PATH_FIELDS), None) or 'lambdas'
    monkeypatch.setattr(owner, 'PATH_FIELDS', {field: PathKind.DIRECTORY})
    pipeline = PipelineConfig.model_validate(raw)

    reached = {
        'declared_paths': {(p.site.resource, p.site.alias, p.site.field) for p in declared_paths(pipeline)},
        'declared_values': {(s.resource, s.alias, s.field) for s, _, _ in declared_values(pipeline, 'PATH_FIELDS')},
    }

    for consumer, sites in reached.items():
        assert (resource, alias, field) in sites, f'{consumer} never saw {resource}/{alias} {field}'


def test_the_scope_guard_accepts_every_resource_its_own_rows_carry():
    # The guard refused `s3` — the word `declared_names` stamps on every `buckets` row — because
    # it walked the members, and `buckets` holds no model to be a member. A door scoped by a
    # name its own output uses got a traceback rather than a refusal.
    pipeline = PipelineConfig.model_validate(
        {
            'buckets': {'raw': 'Bad_Name'},
            'glue_jobs': {'j': {'name': 'n', 'script_bucket': 'b', 'scripts': ['x.py'], 'role': 'r'}},
            'lambdas': {'fn': {'name': 'n', 'source_dir': 'code'}},
        }
    )
    emitted = {p.site.resource for p in pipeline_value_faults('p', pipeline, on_disk=False)}
    emitted |= {n.site.resource for n in declared_names(pipeline)}

    for resource in emitted:
        pipeline_value_faults('p', pipeline, on_disk=False, resource=resource)


def pipeline_rooted_at(root) -> PipelineConfig:
    raw = {'lambdas': {'fn': {'name': 'n', 'source_dir': 'code'}}}
    if root is not None:
        raw['resolve_paths_from'] = root
    return PipelineConfig.model_validate(raw)


def test_the_declared_root_anchors_a_relative_path(tmp_path):
    pipeline = pipeline_rooted_at(str(tmp_path))

    assert resolve_from_root(pipeline, 'modules/lambda/code') == tmp_path / 'modules' / 'lambda' / 'code'


def test_the_declared_root_expands_a_leading_tilde():
    pipeline = pipeline_rooted_at('~/code/salesdata')

    assert resolve_from_root(pipeline, 'code') == Path.home() / 'code' / 'salesdata' / 'code'


def test_paths_resolve_from_the_working_directory_when_no_root_is_set():
    # The behaviour a config that names no root depends on: dectl is run from the checkout and
    # relative paths mean what they would mean to any other command in that shell.
    pipeline = pipeline_rooted_at(None)

    assert resolve_from_root(pipeline, 'code') == Path.cwd() / 'code'


def test_an_absolute_configured_path_ignores_the_root(tmp_path):
    # Mixed absolute and root-relative entries are legal, so an absolute one has to survive
    # being joined onto a root that is nowhere near it.
    pipeline = pipeline_rooted_at(str(tmp_path / 'checkout'))

    assert resolve_from_root(pipeline, '/srv/shared/handler') == Path('/srv/shared/handler')


def test_pipeline_root_is_the_working_directory_when_no_root_is_set():
    assert pipeline_root(pipeline_rooted_at(None)) == Path.cwd()


def test_a_relative_root_is_rejected():
    # A relative root would resolve against the working directory, which is the dependency the
    # key exists to remove — so it fails validation rather than half-working.
    with pytest.raises(ValidationError):
        pipeline_rooted_at('../salesdata')


def test_the_root_is_absent_by_default():
    assert pipeline_rooted_at(None).resolve_paths_from is None


@pytest.mark.parametrize(
    ('source_dir', 'faults'),
    [
        # Relative and climbing out: the anchor is enforced for every value anchored to it, not
        # only the ones a key check reaches for another reason. A lambda builds no S3 key, so
        # `key_fault` never runs on a `source_dir` and only this arm covers it.
        ('..', [ConfigFault.ESCAPES_ROOT]),
        # Caught as an escape rather than as `absent`, so which traversals are refused does not
        # depend on whether an intermediate directory happens to exist.
        ('code/../..', [ConfigFault.ESCAPES_ROOT]),
        ('modules/../code', [ConfigFault.ESCAPES_ROOT]),
        ('code', []),
        # The fourth branch `resolve_from_root` takes. `Path('')` is `.` and `/` drops it, so an
        # empty value resolves to the anchor itself and the deploy uploads the whole checkout.
        ('', [ConfigFault.DECLARES_NOTHING]),
        # Absolute and `~`-rooted values resolve to themselves, so they never claimed the anchor
        # and the escape arm exempts them. A `source_dir` is documented as free to sit anywhere.
        ('ABSOLUTE', []),
        ('~nosuchuser-fixture/code', [ConfigFault.UNRESOLVABLE_HOME]),
    ],
)
def test_every_shape_a_source_dir_can_take_is_judged(tmp_path, source_dir, faults):
    # One row per branch `resolve_from_root` takes — relative, empty, absolute, `~`-rooted —
    # because a fixture carrying four spellings of one branch reads as coverage of all of them.
    (tmp_path / 'code').mkdir()
    (tmp_path / 'code' / 'handler.py').write_text('x')
    if source_dir == 'ABSOLUTE':
        source_dir = str(tmp_path / 'code')
    pipeline = PipelineConfig.model_validate(
        {'resolve_paths_from': str(tmp_path), 'lambdas': {'fn': {'name': 'n', 'source_dir': source_dir}}}
    )

    assert [f.fault for f in pipeline_value_faults('p', pipeline, on_disk=True)] == faults


def test_a_source_under_an_excluded_directory_name_still_holds_its_files(tmp_path):
    # The exclusion is about what sits inside the source, so it is tested below the source and
    # never against the absolute path. Reading the whole path puts every directory above the
    # checkout into the question: a tree that happens to live under one named `__pycache__`
    # reports every lambda source as holding nothing, and the deploy door refuses a directory
    # with a handler in it.
    source = tmp_path / '__pycache__' / 'checkout' / 'code'
    source.mkdir(parents=True)
    (source / 'handler.py').write_text('def handler(event, context): pass')

    assert path_fault(source, PathKind.NON_EMPTY_DIRECTORY) is None
    assert [found.name for found in deployable_files(source)] == ['handler.py']


def test_a_pycache_inside_the_source_is_still_excluded(tmp_path):
    source = tmp_path / 'code'
    (source / '__pycache__').mkdir(parents=True)
    (source / '__pycache__' / 'handler.cpython-313.pyc').write_bytes(b'\x00')

    assert path_fault(source, PathKind.NON_EMPTY_DIRECTORY) is ConfigFault.EMPTY_DIRECTORY


def test_an_absolute_source_dir_still_sits_where_it_likes(tmp_path):
    # The exemption, stated: an absolute or ~-rooted value resolves to itself, so it never
    # claimed the anchor. A source_dir is zipped and uploaded as bytes with no key derived from
    # it, which is why it is allowed outside the root at all.
    outside = tmp_path / 'elsewhere'
    outside.mkdir()
    (outside / 'handler.py').write_text('x')
    pipeline = PipelineConfig.model_validate(
        {'resolve_paths_from': str(tmp_path / 'root'), 'lambdas': {'fn': {'name': 'n', 'source_dir': str(outside)}}}
    )
    (tmp_path / 'root').mkdir()

    assert pipeline_value_faults('p', pipeline, on_disk=True) == []


def test_every_fault_has_a_sentence():
    # FAULT_WORDING is indexed with no default, so a member added without one raises at the
    # moment a reader needs the answer.
    assert set(FAULT_WORDING) == set(ConfigFault)


# Which side of the spelling line each fault falls on, written out rather than derived. A fault
# about how a value is *written* is reported against the configured string, because resolution
# normalises away the very thing it names and a bucket name resolves to nothing at all. A fault
# about what this machine holds carries the resolved path and is answered by `config show`.
SPELLING = frozenset(
    {
        ConfigFault.NOT_A_CLEAN_KEY,
        ConfigFault.KEY_ESCAPES_ROOT,
        ConfigFault.NOT_A_BUCKET_NAME,
    }
)
ON_DISK = frozenset(
    {
        ConfigFault.ABSENT,
        ConfigFault.EXPECTED_DIRECTORY,
        ConfigFault.EXPECTED_FILE,
        ConfigFault.EMPTY_DIRECTORY,
        ConfigFault.DECLARES_NOTHING,
        ConfigFault.UNRESOLVABLE_HOME,
        ConfigFault.ESCAPES_ROOT,
    }
)


def test_every_fault_is_classified_as_a_spelling_or_an_on_disk_one():
    # `SPELLING_FAULTS` is built out of `ConfigFault` members, so `set(ConfigFault) >=` it holds
    # for every subset including the empty one — it cannot fail, and it was the only thing named
    # as pinning the property. The table above can: dropping a member from `SPELLING_FAULTS`
    # fails the first assertion, and adding a fault without deciding its side fails the second.
    #
    # The cost of getting it wrong is silent. A spelling fault outside the set is reported with
    # a resolved path that has the malformation normalised out of it, and its reader is sent to
    # `config show`, which that fault's own remedy says cannot help.
    assert SPELLING_FAULTS == SPELLING
    assert set(ConfigFault) == SPELLING | ON_DISK
    assert not SPELLING & ON_DISK


def rooted_pipeline_naming(tmp_path, source_dir: str) -> PipelineConfig:
    """A pipeline whose root is present, so a fault beneath it is not hidden by an absent one."""
    return PipelineConfig.model_validate({'resolve_paths_from': str(tmp_path), 'lambdas': {'fn': {'name': 'n', 'source_dir': source_dir}}})


@pytest.mark.parametrize('field', ['resolve_paths_from', 'source_dir'])
def test_a_home_this_machine_cannot_resolve_is_a_fault_not_a_load_failure(tmp_path, field):
    pipeline = pipeline_rooted_at('~code/salesdata') if field == 'resolve_paths_from' else rooted_pipeline_naming(tmp_path, '~code/x')
    # `~code/x` is a missing slash, the likeliest typo in a path key. Whether it resolves is a
    # property of this machine, so it belongs where every other such property is checked — see
    # `key_fault`. As a validator it could only reach the values carrying no `{env}`, since the
    # active environment is not known at load; one door answers for both halves.
    faults = pipeline_value_faults('p', pipeline, on_disk=True)

    assert [f.fault for f in faults] == [ConfigFault.UNRESOLVABLE_HOME]


def test_every_config_load_failure_is_one_the_callers_catch():
    # Every caller catches CONFIG_LOAD_ERRORS, so a load failure outside that tuple is one no
    # command recovers from.
    with pytest.raises(CONFIG_LOAD_ERRORS):
        pipeline_rooted_at('../relative')


def test_the_env_token_is_substituted_into_the_root():
    # A token left literal in the root while source_dir substitutes gives a half-resolved join,
    # which is neither what the config says nor what --env asked for.
    set_active_environment('prod', '--env')
    try:
        pipeline = pipeline_rooted_at('/srv/{env}/salesdata')
        assert resolve_from_root(pipeline, 'modules/{env}/code') == Path('/srv/prod/salesdata/modules/prod/code')
    finally:
        set_active_environment('dev', 'default')


def test_a_root_beginning_with_a_token_is_still_rejected_as_relative():
    # The token is stripped rather than substituted for the rootedness test, because --env is
    # parsed after the config loads.
    with pytest.raises(ValidationError):
        pipeline_rooted_at('{env}/salesdata')


@pytest.mark.parametrize('field', ['resolve_paths_from', 'source_dir'])
def test_a_templated_home_resolves_to_a_fault_rather_than_a_traceback(tmp_path, field):
    templated = '~nosuchuser-{env}/code'
    pipeline = pipeline_rooted_at(templated) if field == 'resolve_paths_from' else rooted_pipeline_naming(tmp_path, templated)
    # A `{env}` defers the `~` check past load, and nothing re-validates on the read path:
    # render_env_model runs per resource inside a verb, and validate, show and list never call
    # it. Raising here reached the reader as a traceback; exiting here reached a --json caller
    # as zero bytes on a document its command had already committed to emitting.
    #
    # Both doors, because a guard covering only the half a fault was reported in leaves the
    # sibling silent.
    set_active_environment('prod', '--env')
    try:
        faults = pipeline_value_faults('p', pipeline, on_disk=True)
    finally:
        set_active_environment('dev', 'default')

    assert [(f.site.field, f.fault) for f in faults] == [(field, ConfigFault.UNRESOLVABLE_HOME)]


def test_resolution_never_raises_so_the_renderers_reach_a_faulty_value():
    # The other half of the same property. `config show` and `list` resolve every declared path
    # without running the fault walk first, so a total resolver is what keeps them printing.
    pipeline = pipeline_rooted_at('~nosuchuser/code')

    assert pipeline_root(pipeline) == Path('~nosuchuser/code')
    assert resolve_from_root(pipeline, 'x.py') == Path('~nosuchuser/code/x.py')


def test_a_substituted_value_that_fails_validation_exits_rather_than_raising():
    # render_env_model re-validates, so a config that loaded can still fail inside a verb. A
    # bare ValidationError reaches the reader as a pydantic traceback naming the model rather
    # than the key they typed. No shipped model carries a validator that a substitution can
    # trip, so the boundary is exercised against one written here — an untestable guard and a
    # test that cannot fail are the same defect from two sides.
    class Fussy(StrictModel):
        name: str

        @field_validator('name')
        @classmethod
        def never_prod(cls, value: str) -> str:
            if 'prod' in value:
                raise ValueError('refuses prod')
            return value

    set_active_environment('prod', '--env')
    try:
        with pytest.raises(typer.Exit) as exit_info:
            render_env_model(Fussy(name='sales-{env}'))
        assert exit_info.value.exit_code == 1
    finally:
        set_active_environment('dev', 'default')
