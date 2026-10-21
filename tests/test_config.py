from collections.abc import Mapping
from pathlib import Path
from typing import get_args
from typing import get_origin
from unittest.mock import patch

import pytest
from pydantic import BaseModel
from pydantic import ValidationError

from dectl.config import DectlConfig
from dectl.config import Defaults
from dectl.config import GlueJobConfig
from dectl.config import IcebergTableConfig
from dectl.config import JenkinsConfig
from dectl.config import JenkinsJobConfig
from dectl.config import LambdaConfig
from dectl.config import MonitorConfig
from dectl.config import PipelineConfig
from dectl.config import StepFunctionConfig
from dectl.config import StrictModel
from dectl.config import config_value_faults
from dectl.config import describe_config_error
from dectl.config import load_config
from dectl.config import pipeline_value_faults
from dectl.config import read_config_files
from dectl.config import resolve_from_root
from dectl.values import FAULT_WORDING
from dectl.values import ConfigFault
from dectl.values import DeclaresValues
from dectl.values import Written
from dectl.values import deployable_files
from tests.conftest import LAYERED_CONFIG


def merged(base, overlay):
    if not (isinstance(base, dict) and isinstance(overlay, dict)):
        return overlay
    return {**base, **{key: merged(base.get(key), value) for key, value in overlay.items()}}


def test_variables_substitute_at_load_and_refuse_what_would_clobber(monkeypatch, tmp_path):
    """The committed config fans one top-level variable out to two pipelines through each one's
    own prefix. Its resolved names under an environment are `test_main`'s, read through the CLI."""
    monkeypatch.setattr('dectl.config.CONFIG_PATH', LAYERED_CONFIG)
    config = load_config()
    assert config is not None
    finance = config.pipelines['finance']
    # `{env}` survives the load, because --env is parsed after the tree is built.
    assert finance.buckets['raw'] == 'acme-{env}-finance-raw'
    assert finance.glue_jobs['load'].arguments == {
        'SOURCE': 's3://acme-{env}-finance-raw/invoices/',
        'LANDING': 's3://acme-{env}-finance-raw/invoices/incoming/',
        'TARGET_BUCKET': 'acme-{env}-finance-curated',
        'OUTPUT_PATTERN': 'out/{date}/',
    }

    raw = read_config_files()
    misnamed = DectlConfig.model_validate(merged(raw, {'variables': {'company': 'Acme_Co'}}))
    traced = {(fault.pipeline, fault.written) for fault in config_value_faults(misnamed)}

    def prefix_trace(pipeline: str) -> tuple[str, ...]:
        return (
            f'{{prefix}} from pipelines/{pipeline}.yaml variables.prefix',
            '{company} from config.yaml variables.company',
            f'{{pipeline}} from pipelines/{pipeline}.yaml variables.pipeline',
        )

    # A fault in a bucket named through two variables traces to the one holding the bad character.
    assert traced == {
        row
        for pipeline in ['sales', 'finance']
        for row in [
            (
                pipeline,
                Written('{raw_bucket}', (f'{{raw_bucket}} from pipelines/{pipeline}.yaml variables.raw_bucket', *prefix_trace(pipeline))),
            ),
            (
                pipeline,
                Written(
                    '{curated_bucket}',
                    (f'{{curated_bucket}} from pipelines/{pipeline}.yaml variables.curated_bucket', *prefix_trace(pipeline)),
                ),
            ),
            (pipeline, Written('{prefix}-scripts', prefix_trace(pipeline))),
        ]
    }

    refusals = [
        ('invalid_variable_name', {'variables': {'aws.account': 'x'}}, ''),
        ('unquoted_variable', {'pipelines': {'sales': {'buckets': {'raw': {'prefix': None}}}}}, ''),
        ('reserved_variable', {'variables': {'env': 'x'}}, ''),
        ('shadowed_variable', {'pipelines': {'sales': {'variables': {'account': '9'}}}}, ''),
        (
            'undefined_variable',
            {'pipelines': {'sales': {'variables': {'shared': 'x'}}, 'finance': {'buckets': {'shared': '{shared}'}}}},
            'define it in pipelines/finance.yaml variables too, or move it to config.yaml variables',
        ),
        # Moving it to config.yaml would leave {prefix} out of its reach, so that is not offered.
        (
            'undefined_variable',
            {'pipelines': {'sales': {'variables': {'archive': '{prefix}-archive'}}, 'finance': {'buckets': {'archive': '{archive}'}}}},
            'It is built from the pipeline variables {prefix}, so define {archive} in pipelines/finance.yaml variables too',
        ),
        ('variable_not_a_string', {'variables': {'account': 123}}, 'octal'),
        ('variable_not_a_string', {'variables': {'glue': {'role': 'r'}}}, 'YAML read this as a mapping'),
    ]
    for kind, overlay, message in refusals:
        with pytest.raises(ValidationError) as caught:
            DectlConfig.model_validate(merged(raw, overlay))
        assert [error['type'] for error in caught.value.errors()] == [kind]
        assert message in caught.value.errors()[0]['msg']


def test_load_config_returns_none_when_file_missing():
    with patch('dectl.config.CONFIG_PATH', Path('/nonexistent/config.yaml')):
        assert load_config() is None


def test_each_pipeline_is_read_from_its_own_file(monkeypatch, tmp_path):
    """config.yaml's variables reach every pipeline file and a pipeline's reach its own, and a
    fault names the file holding it."""
    monkeypatch.setattr('dectl.config.CONFIG_PATH', tmp_path / 'config.yaml')
    (tmp_path / 'config.yaml').write_text('variables:\n  company: acme\ndefaults:\n  account_id: "1"\n')
    pipelines = tmp_path / 'pipelines'
    pipelines.mkdir()
    (pipelines / 'sales.yaml').write_text('variables:\n  raw: "{company}-sales-raw"\nbuckets:\n  raw: "{raw}"\n')
    (pipelines / 'finance.yaml').write_text('buckets:\n  raw: "{company}-finance-raw"\n')
    # An editor's swap and backup files are skipped, never refused with every pipeline.
    (pipelines / '.finance.yaml.swp').write_text('[ not yaml')
    (pipelines / 'finance.yaml~').write_text('[ not yaml')

    config = load_config()
    assert config is not None
    assert {name: pipeline.buckets for name, pipeline in config.pipelines.items()} == {
        'finance': {'raw': 'acme-finance-raw'},
        'sales': {'raw': 'acme-sales-raw'},
    }

    (pipelines / 'finance.yaml').write_text('buckets:\n  raw: "{raw}"\n')
    (pipelines / 'orders.yml').write_text('buckets: {}\n')
    (pipelines / 'search.yaml').write_text('buckets: {}\n')
    with pytest.raises(ValidationError) as caught:
        load_config()
    # A pipeline saved as .yml, and one dectl's own `search` would hide, are each refused by name.
    assert [(error['type'], error['input']) for error in caught.value.errors()] == [
        ('not_a_pipeline_file', 'orders.yml'),
        ('reserved_pipeline_name', 'search.yaml'),
    ]
    (pipelines / 'orders.yml').unlink()
    (pipelines / 'search.yaml').unlink()
    with pytest.raises(ValidationError) as caught:
        load_config()
    assert describe_config_error(caught.value)[0].startswith(
        '  pipelines/finance.yaml buckets.raw: variable {raw} is defined in pipelines/sales.yaml variables, which this value cannot see'
    )

    # A file still wrapped in its pipeline's name defines no variables, and that key is the one line to fix.
    (pipelines / 'sales.yaml').write_text('sales:\n  variables:\n    raw: "{company}-sales-raw"\n  buckets:\n    raw: "{raw}"\n')
    with pytest.raises(ValidationError) as caught:
        load_config()
    assert describe_config_error(caught.value)[0].startswith('  pipelines/sales.yaml sales: not a key this file takes')
    assert len(caught.value.errors()) == 1


def glue_job(**sizing):
    return GlueJobConfig(name='j', script_bucket='sales-scripts', scripts=['s.py'], role='r', **sizing)


def test_a_job_sizing_itself_both_ways_is_refused_by_name():
    # Glue refuses this too, but from UpdateJob, which the deploy runs after the scripts are
    # already uploaded — and its message names neither the job nor the config key.
    with pytest.raises(ValidationError) as raised:
        glue_job(max_capacity=1, worker_type='G.1X', number_of_workers=2)

    message = str(raised.value)
    assert 'max_capacity' in message
    assert 'number_of_workers, worker_type' in message
    assert 'j' in message


@pytest.mark.parametrize(
    'named, missing',
    [({'worker_type': 'G.1X'}, 'number_of_workers'), ({'number_of_workers': 2}, 'worker_type')],
)
def test_half_a_worker_pair_is_refused_naming_the_other_half(named, missing):
    # Glue sizes by the pair. One without the other reaches UpdateJob as a half-specified job,
    # and which half is missing is the whole content of the fix.
    with pytest.raises(ValidationError) as raised:
        glue_job(**named)

    assert missing in str(raised.value)


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

    assert lines[0] == (
        '  pipelines/p.yaml step_function: not a key this file takes at its top level, which are resolve_paths_from, '
        'variables, glue_jobs, lambdas, step_functions, buckets, iceberg_tables, monitor, jenkins'
    )
    assert lines[1] == "    got: {'flow': {'name': 'sm'}}"


def test_describe_truncates_a_large_rejected_value():
    # A `missing` error's input is the whole parent object, so a real pipeline block would
    # bury the message it is meant to illustrate.
    exc = validation_error_for({'pipelines': {'p': {'buckets': {f'alias-{n}': f'bucket-{n}' for n in range(40)}}}})

    lines = describe_config_error(exc)

    assert lines[0] == '  config.yaml defaults: Field required'
    assert lines[1].endswith('…')
    assert len(lines[1]) < 200


def test_describe_reports_every_problem_not_only_the_first():
    exc = validation_error_for({'defaults': {'account_id': '111', 'acct': 'x'}, 'pipelines': {'p': {'glue_jbs': {}}}})

    lines = describe_config_error(exc)

    # Each names the file holding it, so the two land in different files.
    assert '  config.yaml defaults.acct: Extra inputs are not permitted' in lines
    assert any(line.startswith('  pipelines/p.yaml glue_jbs: not a key this file takes') for line in lines)


# Every string-valued config field that dectl deliberately does not check, with the reason. A
# new field lands in the assertion below rather than in here by default, so adding one is a
# decision about which of the three declarations it belongs in — or an entry here saying why it
# belongs in none. A name-based heuristic cannot do that job: it read six words plus a literal
# `scripts`, so `asl`, `handler`, `template`, `manifest` and `entrypoint` were all invisible.
UNCHECKED_FIELDS = {
    (DectlConfig, 'variables'): 'substituted into the fields that use it, which are checked as they are',
    (PipelineConfig, 'variables'): 'substituted into the fields that use it, which are checked as they are',
    (Defaults, 'account_id'): 'the AWS account; every call is made against it and AWS reports a mismatch',
    (Defaults, 'region'): 'an AWS region; botocore rejects one that does not exist',
    (Defaults, 'environment'): 'the default {env} substitution, which names nothing on its own',
    (Defaults, 'aws_profile'): 'a local AWS profile; make_session is where a missing one is reported',
    (JenkinsConfig, 'url'): 'a Jenkins base URL; the request is what fails on a wrong one',
    (JenkinsConfig, 'user'): 'a Jenkins username; Jenkins authenticates it',
    (JenkinsConfig, 'token'): 'a Jenkins API token; Jenkins authenticates it',
    (JenkinsJobConfig, 'job_path'): 'a path within Jenkins, not on this machine or in S3',
    (JenkinsJobConfig, 'parameters'): 'build parameters posted to Jenkins verbatim; Jenkins rejects a name its job does not take',
    (JenkinsJobConfig, 'actions'): 'per-action build parameters posted to Jenkins verbatim; Jenkins rejects a name its job does not take',
    (GlueJobConfig, 'name'): 'a Glue job name; GetJob is what says whether it exists',
    (GlueJobConfig, 'role'): 'an IAM role ARN; UpdateJob validates it',
    (GlueJobConfig, 'arguments'): 'Glue job arguments, passed through; Glue validates them',
    (GlueJobConfig, 'connections'): 'Glue connection names; UpdateJob validates them',
    (GlueJobConfig, 'worker_type'): 'a Glue worker type; AWS owns the list and adds to it, so UpdateJob is the authority',
    (GlueJobConfig, 'glue_version'): 'a Glue version; AWS owns the list and adds to it, so UpdateJob is the authority',
    (GlueJobConfig, 'python_version'): 'the Python a Glue command runs; UpdateJob rejects one the job type does not offer',
    (GlueJobConfig, 'execution_class'): 'STANDARD or FLEX; UpdateJob rejects anything else',
    (LambdaConfig, 'name'): 'a Lambda function name; the API is what says whether it exists',
    (LambdaConfig, 'live_alias'): 'a Lambda alias; update_alias validates it',
    (StepFunctionConfig, 'name'): 'a state machine name; the API is what says whether it exists',
    (StepFunctionConfig, 'log_group'): 'a CloudWatch log group; monitor reports it unset',
    (
        StepFunctionConfig,
        'definition_substitutions',
    ): 'values written into the definition; ValidateStateMachineDefinition judges the result',
    (StepFunctionConfig, 'live_alias'): 'a state machine alias; deploy --publish reports it absent before writing anything',
    (IcebergTableConfig, 'database'): 'a Glue Data Catalog database; GetTable reports its absence',
    (IcebergTableConfig, 'table'): 'a Glue Data Catalog table; GetTable reports its absence',
    (MonitorConfig, 'lambdas'): 'aliases into this pipeline; build_monitor_sources warns on a dangling one',
    (MonitorConfig, 'step_functions'): 'aliases into this pipeline; build_monitor_sources warns on a dangling one',
}


# What a declared field's value may be: one string, or a list or mapping of them. Anything else
# is not a value dectl resolves.
def carries_strings(annotation) -> bool:
    """Whether this annotation's values are strings dectl might have to resolve.

    Derived from the annotation rather than matched against a list of spellings. A list of the
    forms in use today is a narrower tree than the models hold: a string-bearing field written
    any other way — `tuple[str, ...]`, `set[str]`, a nested container — is invisible to the
    guard below, which then reports every field classified while one is checked by nothing.

    A mapping is judged by its value type, so `dict[str, str]` carries strings and
    `dict[str, GlueJobConfig]` does not: the key there names an alias rather than a value the
    config points at something with."""
    if annotation is str:
        return True
    args = [a for a in get_args(annotation) if a is not type(None)]
    if not args:
        return False
    origin = get_origin(annotation)
    if origin in (dict, Mapping):
        return carries_strings(args[-1])
    return any(carries_strings(arg) for arg in args)


def string_fields(model: type[BaseModel]) -> set[str]:
    return {name for name, field in model.model_fields.items() if carries_strings(field.annotation)}


def config_models(root: type[BaseModel] = DectlConfig) -> set[type[StrictModel]]:
    """Every model the config tree holds, from the top of it down.

    Seeded from `DectlConfig` rather than from `PipelineConfig`, and recursive. Seeding from the
    pipeline put `Defaults`, `JenkinsConfig` and `JenkinsJobConfig` outside the walk entirely,
    so a string field on any of them was unclassified and unreported — and `aws_profile` is the
    field that decides which AWS account a deploy reaches.

    Both a bare annotation and a `dict[str, Model]` one, because the code reaches both. Walking
    only `get_args` left a declaration on `monitor` — a bare `MonitorConfig` field — inert with
    the whole suite green, which is the guard reporting complete on a member it cannot see.

    The root is seeded into the result, not only into the queue. Nothing declares a field of the
    root's type, so a walk that adds only what it finds in an annotation never reaches it, and
    every field on `DectlConfig` is then outside every guard reading this."""
    found: set[type[StrictModel]] = {root} if issubclass(root, StrictModel) else set()
    pending = [root]
    while pending:
        model = pending.pop()
        for field in model.model_fields.values():
            for arg in [field.annotation, *get_args(field.annotation)]:
                if isinstance(arg, type) and issubclass(arg, StrictModel) and arg not in found:
                    found.add(arg)
                    pending.append(arg)
    return found


def test_every_string_field_is_classified():
    # The invariant the design rests on: three declarations say what dectl checks about a field,
    # and everything that checks, resolves or excludes a value reads one of them. A field in
    # none of them and not named above is checked by nothing, and that reads as success — the
    # config validates and the env-effect guard goes quiet.
    unclassified = set()
    for model in config_models():
        declared = set()
        if issubclass(model, DeclaresValues):
            declared = set(model.PATH_FIELDS) | model.KEY_FIELDS | model.BUCKET_FIELDS
        for field in string_fields(model) - declared:
            unclassified.add((model, field))

    assert unclassified - set(UNCHECKED_FIELDS) == set()


def pipeline_rooted_at(root) -> PipelineConfig:
    raw = {'lambdas': {'fn': {'name': 'n', 'source_dir': 'code'}}}
    if root is not None:
        raw['resolve_paths_from'] = root
    return PipelineConfig.model_validate(raw)


def test_the_declared_root_anchors_a_relative_path(tmp_path):
    pipeline = pipeline_rooted_at(str(tmp_path))

    assert resolve_from_root(pipeline, 'modules/lambda/code') == tmp_path / 'modules' / 'lambda' / 'code'


@pytest.mark.parametrize('root', ['../salesdata', 'code/salesdata', '{env}/salesdata', ''])
def test_a_root_that_is_not_rooted_is_reported_rather_than_raised(root):
    # A relative root resolves against the working directory, which is the dependency the key
    # exists to remove. It is reported for the reason every other spelling fault is: a schema
    # failure sends `main.py` to `cfg = None` and takes every pipeline command with it, so the
    # likeliest first spelling of a new key would blank the CLI that explains it.
    #
    # A leading `{env}` is relative whatever the environment resolves to, and `--env` is parsed
    # after the config loads, so the token is replaced with a plain name rather than substituted.
    pipeline = pipeline_rooted_at(root)

    faults = pipeline_value_faults('p', pipeline, on_disk=False)

    assert [(f.site.field, f.fault) for f in faults] == [('resolve_paths_from', ConfigFault.NOT_A_ROOTED_PATH)]


@pytest.mark.parametrize(
    ('source_dir', 'faults'),
    [
        # Relative and landing outside: the anchor is enforced for every value anchored to it,
        # not only the ones a key check reaches for another reason. A lambda builds no S3 key,
        # so `key_fault` never runs on a `source_dir` and only this arm covers it.
        ('..', [ConfigFault.ESCAPES_ROOT]),
        # Judged by where it lands rather than by how it is written, so a traversal that stays
        # inside the root is not refused for leaving it — the row's own resolved path would
        # disprove the fault name, and the remedy would describe what the config already does.
        ('code/../..', [ConfigFault.ESCAPES_ROOT]),
        ('modules/../code', []),
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


def test_every_fault_has_a_sentence():
    # FAULT_WORDING is indexed with no default, so a member added without one raises at the
    # moment a reader needs the answer.
    assert set(FAULT_WORDING) == set(ConfigFault)


def test_a_bucket_name_is_checked_on_a_pipeline_that_declares_no_root():
    # Bucket spelling is a property of the config, not of this machine, so it is answerable with
    # no anchor and with `on_disk` false. Moving the loop below the `on_disk` gate left the suite
    # green while this config went from two refusals and exit 3 to `is valid` and exit 0.
    pipeline = PipelineConfig.model_validate({'buckets': {'raw': 'My_Bad_Bucket', 'curated': 'also..bad'}})

    faults = pipeline_value_faults('p', pipeline, on_disk=False)

    assert {problem.site.field for problem in faults} == {'buckets'}
    assert {problem.fault for problem in faults} == {ConfigFault.NOT_A_BUCKET_NAME}
    assert len(faults) == 2


def test_a_relative_path_is_not_an_escape_when_no_anchor_is_declared(tmp_path):
    # Without `resolve_paths_from` the root is the working directory and `../shared/code` is an
    # ordinary path that has always been followed. Enforcing the anchor where none is declared
    # refuses a config that deploys, which is the one direction this check must not fail in.
    pipeline = PipelineConfig.model_validate({'lambdas': {'fn': {'name': 'n', 'source_dir': '../shared/code'}}})

    assert pipeline_value_faults('p', pipeline, on_disk=False) == []


def test_a_source_dir_naming_the_checkout_does_not_ship_its_git_directory(tmp_path):
    # `.` resolves to the anchor, and on an https remote `.git/config` carries a credential
    # helper line. Excluded by name wherever it sits below the source, so a submodule's `.git`
    # under an otherwise ordinary source directory is covered by the same rule.
    (tmp_path / '.git').mkdir()
    (tmp_path / '.git' / 'config').write_text('[credential]\n  helper = store\n')
    (tmp_path / 'code').mkdir()
    (tmp_path / 'code' / 'handler.py').write_text('x')

    shipped = {str(found.relative_to(tmp_path)) for found in deployable_files(tmp_path)}

    assert shipped == {'code/handler.py'}


def test_one_fault_value_publishes_one_row_shape(tmp_path):
    """A `~` naming nobody resolves to nothing at either site, so both rows say so.

    The root row published the configured string in the `path` key while every leaf row
    published null, which gives a `--json` consumer branching on `path is None` two answers to
    one `fault`. The leaf row named no value at all, so the missing slash the fault exists to
    point at was on screen nowhere."""
    leaf = PipelineConfig.model_validate(
        {'resolve_paths_from': str(tmp_path), 'lambdas': {'fn': {'name': 'n', 'source_dir': '~nosuchuser/code'}}}
    )
    root = PipelineConfig.model_validate({'resolve_paths_from': '~nosuchuser/code'})

    rows = pipeline_value_faults('a', leaf, on_disk=True) + pipeline_value_faults('b', root, on_disk=True)

    assert {row.fault for row in rows} == {ConfigFault.UNRESOLVABLE_HOME}
    assert [row.path for row in rows] == [None, None]
    # Shown rather than swallowed: the characters are the finding, so both rows quote them.
    assert all(row.shown == repr('~nosuchuser/code') for row in rows)
