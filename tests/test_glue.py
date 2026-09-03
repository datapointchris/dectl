from pathlib import Path

import pytest
import typer

from dectl import prompt
from dectl.commands.glue import GlueRunWatcher
from dectl.commands.glue import ResolvedScript
from dectl.commands.glue import apply_glue_job_update
from dectl.commands.glue import build_job_update
from dectl.commands.glue import configured_uris
from dectl.commands.glue import follow_glue_run
from dectl.commands.glue import job_definition_changes
from dectl.commands.glue import make_glue_app
from dectl.commands.glue import plan_glue_job_update
from dectl.commands.glue import resolve_scripts
from dectl.commands.glue import upload_scripts
from dectl.config import ROOT_SITE
from dectl.config import DectlConfig
from dectl.config import GlueJobConfig
from dectl.config import PipelineConfig
from dectl.config import config_value_faults
from dectl.config import pipeline_value_faults
from dectl.env import active_environment
from dectl.env import render_env_model
from dectl.pipeline_view import script_uris
from dectl.values import ConfigFault
from dectl.values import ValueSite
from dectl.values import bucket_fault
from tests.conftest import RefusalRunner

runner = RefusalRunner()


def test_job_exposes_deploy_run_logs_runs_verbs():
    config = DectlConfig.model_validate(
        {
            'defaults': {'account_id': '123456789012'},
            'pipelines': {
                'proj': {'glue_jobs': {'source-copy': {'name': 'j', 'script_bucket': 'sales-scripts', 'scripts': ['s.py'], 'role': 'r'}}}
            },
        }
    )
    app = make_glue_app('proj', config.pipelines['proj'], config)

    result = runner.invoke(app, ['source-copy', '--help'])

    assert result.exit_code == 0
    for verb in ('deploy', 'run', 'logs', 'runs'):
        assert verb in result.stdout


class FakeGlueClient:
    def __init__(self, existing_job):
        self.existing_job = existing_job
        self.captured_update = None

    def get_job(self, JobName):
        return {'Job': self.existing_job}

    def update_job(self, JobName, JobUpdate):
        self.captured_update = JobUpdate
        return {}


class FakeSession:
    def __init__(self, glue_client):
        self.glue_client = glue_client

    def client(self, name):
        assert name == 'glue'
        return self.glue_client


def make_job(connections=None, arguments=None, max_capacity=None):
    return GlueJobConfig(
        name='my-job',
        script_bucket='my-bucket',
        scripts=['main.py'],
        role='arn:aws:iam::123456789012:role/glue',
        connections=connections,
        arguments=arguments or {},
        max_capacity=max_capacity,
    )


def apply(glue, job):
    """The sequence `deploy` runs, so these tests exercise the path the CLI takes."""
    session = FakeSession(glue)
    job_update = plan_glue_job_update(session, job, assume_yes=True)
    if job_update is not None:
        apply_glue_job_update(session, job, job_update)


def test_update_omits_connections_when_job_has_none():
    # Glue's get_job omits the Connections key entirely for a job with no connections.
    glue = FakeGlueClient(existing_job={'Command': {'Name': 'pythonshell'}})
    apply(glue, make_job())

    assert 'Connections' not in glue.captured_update


def test_update_includes_connections_when_configured():
    glue = FakeGlueClient(existing_job={'Command': {'Name': 'pythonshell'}})
    apply(glue, make_job(connections=['vpc-conn']))

    assert glue.captured_update['Connections'] == {'Connections': ['vpc-conn']}


def test_configured_connections_replace_rather_than_merge():
    # A merge would make a stale config entry immortal: a connection renamed in Terraform would
    # be silently reattached under its old name on every deploy.
    existing = {'Command': {'Name': 'pythonshell'}, 'Connections': {'Connections': ['old-conn']}}
    glue = FakeGlueClient(existing_job=existing)
    apply(glue, make_job(connections=['new-conn']))

    assert glue.captured_update['Connections'] == {'Connections': ['new-conn']}


def test_empty_connections_list_detaches_every_connection():
    existing = {'Command': {'Name': 'pythonshell'}, 'Connections': {'Connections': ['vpc-conn']}}
    glue = FakeGlueClient(existing_job=existing)
    apply(glue, make_job(connections=[]))

    assert 'Connections' not in glue.captured_update


def test_unset_connections_leaves_the_jobs_own_connections_alone():
    existing = {'Command': {'Name': 'pythonshell'}, 'Connections': {'Connections': ['vpc-conn']}}
    glue = FakeGlueClient(existing_job=existing)
    apply(glue, make_job())

    assert glue.captured_update['Connections'] == {'Connections': ['vpc-conn']}


def test_max_capacity_is_set_when_configured():
    glue = FakeGlueClient(existing_job={'Command': {'Name': 'pythonshell'}, 'MaxCapacity': 0.0625})
    apply(glue, make_job(max_capacity=1.0))

    assert glue.captured_update['MaxCapacity'] == 1.0


def test_max_capacity_on_a_worker_based_job_is_rejected():
    existing = {'Command': {'Name': 'glueetl'}, 'WorkerType': 'G.1X', 'NumberOfWorkers': 2}
    glue = FakeGlueClient(existing_job=existing)

    with pytest.raises(typer.Exit):
        apply(glue, make_job(max_capacity=1.0))


def test_update_without_a_terminal_fails_naming_the_flag():
    """A prompt on a stdin that never closes deadlocks a Jenkins step or a cron
    run with no output and no exit code. pytest's stdin is not a terminal, which
    is the case."""
    glue = FakeGlueClient(existing_job={'Command': {'Name': 'pythonshell'}})

    with pytest.raises(typer.Exit) as raised:
        plan_glue_job_update(FakeSession(glue), make_job())

    assert raised.value.exit_code == 1
    assert glue.captured_update is None


def test_no_input_refuses_the_prompt_even_on_a_terminal(monkeypatch):
    monkeypatch.setattr('sys.stdin.isatty', lambda: True)
    monkeypatch.setattr(prompt.interactivity, 'no_input', True)
    glue = FakeGlueClient(existing_job={'Command': {'Name': 'pythonshell'}})

    with pytest.raises(typer.Exit):
        plan_glue_job_update(FakeSession(glue), make_job())

    assert glue.captured_update is None


def test_update_is_skipped_when_nothing_dectl_manages_changed():
    # The steady state once Terraform owns the job: deploy becomes a pure code push.
    job = make_job(connections=['vpc-conn'], arguments={'SOURCE': 'b'}, max_capacity=1.0)
    existing = {
        'Command': {'Name': 'pythonshell', 'ScriptLocation': 's3://my-bucket/scripts/main.py'},
        'Role': 'arn:aws:iam::123456789012:role/glue',
        'Connections': {'Connections': ['vpc-conn']},
        'DefaultArguments': {'--JOB_NAME': 'my-job', '--SOURCE': 'b'},
        'MaxCapacity': 1.0,
    }
    glue = FakeGlueClient(existing_job=existing)
    apply(glue, job)

    assert glue.captured_update is None


def test_changes_report_a_detached_connection_as_a_removal():
    existing = {'Command': {'Name': 'pythonshell'}, 'Connections': {'Connections': ['old-conn']}}
    job_update = build_job_update(existing, make_job(connections=[]))

    changes = job_definition_changes(existing, job_update)

    assert ('Connections', "{'Connections': ['old-conn']}", '(removed)') in changes


def test_changes_expand_default_arguments_per_key():
    existing = {'Command': {'Name': 'pythonshell'}, 'DefaultArguments': {'--SOURCE_PREFIX': 'incoming'}}
    job_update = build_job_update(existing, make_job(arguments={'SOURCE_PREFIX': 'landing'}))

    changes = job_definition_changes(existing, job_update)

    assert ('DefaultArguments.--SOURCE_PREFIX', 'incoming', 'landing') in changes


def test_update_preserves_unmanaged_job_fields():
    # UpdateJob replaces the definition, so fields dectl does not manage must be carried over
    # from the existing job or they silently reset to defaults.
    existing = {
        'Command': {'Name': 'pythonshell', 'PythonVersion': '3.9'},
        'Timeout': 60,
        'GlueVersion': '3.0',
        'MaxRetries': 2,
        'ExecutionProperty': {'MaxConcurrentRuns': 5},
    }
    glue = FakeGlueClient(existing_job=existing)
    apply(glue, make_job())

    update = glue.captured_update
    assert update['Timeout'] == 60
    assert update['GlueVersion'] == '3.0'
    assert update['MaxRetries'] == 2
    assert update['ExecutionProperty'] == {'MaxConcurrentRuns': 5}
    assert update['Command']['PythonVersion'] == '3.9'


def test_update_strips_read_only_keys():
    existing = {
        'Name': 'my-job',
        'Command': {'Name': 'pythonshell'},
        'CreatedOn': 'ts',
        'LastModifiedOn': 'ts',
        'ProfileName': 'default',
        'AllocatedCapacity': 2,
    }
    glue = FakeGlueClient(existing_job=existing)
    apply(glue, make_job())

    for key in ('Name', 'CreatedOn', 'LastModifiedOn', 'ProfileName', 'AllocatedCapacity'):
        assert key not in glue.captured_update


def test_update_overrides_role_and_script_location():
    existing = {'Role': 'old-role', 'Command': {'Name': 'pythonshell', 'ScriptLocation': 's3://old/x.py'}}
    glue = FakeGlueClient(existing_job=existing)
    apply(glue, make_job())

    assert glue.captured_update['Role'] == 'arn:aws:iam::123456789012:role/glue'
    assert glue.captured_update['Command']['ScriptLocation'] == 's3://my-bucket/scripts/main.py'


def test_update_merges_default_arguments_onto_existing():
    existing = {
        'Command': {'Name': 'pythonshell'},
        'DefaultArguments': {'--TempDir': 's3://tmp/', '--enable-glue-datacatalog': 'true'},
    }
    glue = FakeGlueClient(existing_job=existing)
    apply(glue, make_job(arguments={'extra-flag': 'on'}))

    args = glue.captured_update['DefaultArguments']
    assert args['--TempDir'] == 's3://tmp/'  # preserved from existing
    assert args['--enable-glue-datacatalog'] == 'true'  # preserved from existing
    assert args['--JOB_NAME'] == 'my-job'  # always set by dectl
    assert args['--extra-flag'] == 'on'  # added from config, -- prefix applied


class FakeRunGlueClient:
    """Glue stand-in that walks a run through a scripted sequence of states."""

    def __init__(self, states: list[str]) -> None:
        self.states = states
        self.get_run_calls = 0

    def start_job_run(self, JobName):
        return {'JobRunId': 'jr_test'}

    def get_job_run(self, JobName, RunId):
        self.get_run_calls += 1
        state = self.states[min(self.get_run_calls - 1, len(self.states) - 1)]
        return {'JobRun': {'JobRunState': state}}


class FakeRunSession:
    def __init__(self, glue_client) -> None:
        self.glue_client = glue_client

    def client(self, name):
        if name == 'glue':
            return self.glue_client
        return FakeEmptyLogsClient()


class FakeEmptyLogsClient:
    def filter_log_events(self, **kwargs):
        return {'events': []}


def test_watcher_reports_a_run_as_finished_only_in_a_terminal_state():
    glue = FakeRunGlueClient(['RUNNING', 'SUCCEEDED'])
    watcher = GlueRunWatcher(glue, 'my-job', 'jr_test')

    assert watcher.finished() is False
    assert watcher.finished() is True
    assert watcher.state == 'SUCCEEDED'


def test_following_a_failed_run_exits_non_zero(monkeypatch):
    # A followed run that fails must be distinguishable from one that succeeded without reading
    # the log text, so `dectl ... run --follow && next-step` behaves.
    monkeypatch.setattr('dectl.logs.time.sleep', lambda _seconds: None)
    session = FakeRunSession(FakeRunGlueClient(['FAILED']))

    with pytest.raises(typer.Exit) as exit_info:
        follow_glue_run(session, make_job(), 'jr_test')

    assert exit_info.value.exit_code == 1


def test_following_a_successful_run_returns_cleanly(monkeypatch):
    monkeypatch.setattr('dectl.logs.time.sleep', lambda _seconds: None)
    session = FakeRunSession(FakeRunGlueClient(['SUCCEEDED']))

    follow_glue_run(session, make_job(), 'jr_test')


class FakeS3Client:
    """S3 stand-in that refuses a Filename it cannot read, exactly as upload_file does.

    A fake accepting any string would let a resolver that produces a wrong path look identical
    to one that produces the right path, which is the whole of what these tests measure."""

    def __init__(self) -> None:
        self.uploads: list[tuple[str, str, str]] = []

    def upload_file(self, **kwargs):
        filename = kwargs['Filename']
        if not Path(filename).is_file():
            raise FileNotFoundError(filename)
        # Real S3 answers InvalidBucketName for anything outside its naming rule, so a fake that
        # took any string would let an unsubstituted `b-{env}` look identical to a substituted
        # one. The rule is `bucket_fault`, read rather than restated: a second copy of the same
        # expression here could not disagree with it on any input, so it proved nothing while
        # reading as an independent reading. `BUCKET_NAMES` is what holds the rule honest — a
        # table written from S3's documentation and measured against the check and this fake.
        bucket = kwargs['Bucket']
        if bucket_fault(bucket) is not None:
            raise ValueError(f'InvalidBucketName: {bucket}')
        self.uploads.append((filename, bucket, kwargs['Key']))


class FakeS3Session:
    def __init__(self, s3_client):
        self.s3_client = s3_client

    def client(self, name):
        assert name == 's3'
        return self.s3_client


def glue_pipeline(root, scripts) -> PipelineConfig:
    # A real bucket name, because FakeS3Client applies S3's own naming rule. 'b' is below the
    # three-character minimum and would be rejected by the service.
    raw = {'glue_jobs': {'j': {'name': 'my-job', 'script_bucket': 'salesdata-scripts', 'scripts': scripts, 'role': 'r'}}}
    if root is not None:
        raw['resolve_paths_from'] = root
    return PipelineConfig.model_validate(raw)


def test_scripts_upload_from_the_repo_rather_than_the_working_directory(tmp_path):
    (tmp_path / 'jobs').mkdir()
    (tmp_path / 'jobs' / 'copy.py').write_text('print("hi")')
    pipeline = glue_pipeline(str(tmp_path), ['jobs/copy.py'])
    job = pipeline.glue_jobs['j']
    s3 = FakeS3Client()

    upload_scripts(FakeS3Session(s3), job, resolve_scripts('proj', pipeline, 'j'))

    assert s3.uploads == [(str(tmp_path / 'jobs' / 'copy.py'), 'salesdata-scripts', 'scripts/jobs/copy.py')]


def test_the_s3_key_keeps_the_configured_path_not_the_resolved_one(tmp_path):
    # The key is what the Glue job's ScriptLocation points at, so anchoring it to a machine's
    # checkout would make the uploaded object's name depend on who ran the deploy.
    (tmp_path / 'copy.py').write_text('x')
    pipeline = glue_pipeline(str(tmp_path), ['copy.py'])
    job = pipeline.glue_jobs['j']
    s3 = FakeS3Client()

    upload_scripts(FakeS3Session(s3), job, resolve_scripts('proj', pipeline, 'j'))

    assert s3.uploads[0][2] == 'scripts/copy.py'


def test_the_upload_and_the_job_definition_name_one_object(tmp_path, monkeypatch):
    # Three sites build this key. Any two disagreeing means Glue fetches an object nothing
    # uploaded, and a surviving {env} is the way they diverge.
    monkeypatch.setattr(active_environment, 'name', 'prod')
    (tmp_path / 'jobs' / 'prod').mkdir(parents=True)
    (tmp_path / 'jobs' / 'prod' / 'copy.py').write_text('x')
    pipeline = glue_pipeline(str(tmp_path), ['jobs/{env}/copy.py'])
    # Rendered, as `deploy` hands it to both: `script_uri` and the upload take a job whose
    # operands are substituted, so a raw job here would test a shape the verb never produces.
    job = render_env_model(pipeline.glue_jobs['j'])
    s3 = FakeS3Client()

    upload_scripts(FakeS3Session(s3), job, resolve_scripts('proj', pipeline, 'j'))
    definition = build_job_update({'Name': 'my-job'}, job)

    assert definition['Command']['ScriptLocation'] == f's3://{s3.uploads[0][1]}/{s3.uploads[0][2]}'
    assert '{env}' not in definition['Command']['ScriptLocation']


def test_extra_py_files_name_the_same_objects_the_upload_wrote(tmp_path, monkeypatch):
    monkeypatch.setattr(active_environment, 'name', 'prod')
    (tmp_path / 'prod').mkdir()
    for leaf in ('main.py', 'util.py'):
        (tmp_path / 'prod' / leaf).write_text('x')
    pipeline = glue_pipeline(str(tmp_path), ['{env}/main.py', '{env}/util.py'])
    job = render_env_model(pipeline.glue_jobs['j'])
    s3 = FakeS3Client()

    upload_scripts(FakeS3Session(s3), job, resolve_scripts('proj', pipeline, 'j'))
    definition = build_job_update({'Name': 'my-job'}, job)

    assert definition['DefaultArguments']['--extra-py-files'] == f's3://{s3.uploads[1][1]}/{s3.uploads[1][2]}'


@pytest.mark.parametrize(
    ('written', 'expected'),
    [
        ('/srv/shared/handler.py', ConfigFault.KEY_ESCAPES_ROOT),
        ('../sibling/util.py', ConfigFault.KEY_ESCAPES_ROOT),
        ('~/shared/lib.py', ConfigFault.KEY_ESCAPES_ROOT),
        ('./copy.py', ConfigFault.NOT_A_CLEAN_KEY),
        ('jobs//copy.py', ConfigFault.NOT_A_CLEAN_KEY),
        ('jobs/./copy.py', ConfigFault.NOT_A_CLEAN_KEY),
        ('copy.py/', ConfigFault.NOT_A_CLEAN_KEY),
    ],
)
def test_a_key_that_s3_would_store_literally_is_refused(tmp_path, written, expected):
    # Each shape is pinned to its own fault, not to "one of the two". `validate --json`
    # publishes the name, so an assertion satisfied by either value pins neither — an
    # unreachable NOT_A_CLEAN_KEY would satisfy it for every input here.
    #
    # A leading ~ is KEY_ESCAPES_ROOT rather than a spelling fault: it resolves through $HOME,
    # which is the same dependence on where the deploy ran that `resolve_paths_from` removes.
    # The key-specific member is what carries the S3-key sentence; a `source_dir` leaving the
    # anchor gets ESCAPES_ROOT, whose remedy allows the `~/x` this one refuses.
    pipeline = glue_pipeline(str(tmp_path), [written])

    assert [f.fault for f in pipeline_value_faults('proj', pipeline, on_disk=True)] == [expected]


@pytest.mark.parametrize(
    ('written', 'expected'),
    [
        ('/scripts', ConfigFault.KEY_ESCAPES_ROOT),
        ('../scripts', ConfigFault.KEY_ESCAPES_ROOT),
        ('~/scripts', ConfigFault.KEY_ESCAPES_ROOT),
        ('./scripts', ConfigFault.NOT_A_CLEAN_KEY),
        ('scripts/', ConfigFault.NOT_A_CLEAN_KEY),
        ('scripts//sub', ConfigFault.NOT_A_CLEAN_KEY),
    ],
)
def test_a_script_prefix_is_refused_for_every_shape_a_script_is(tmp_path, written, expected):
    # The prefix is the other operand of the concatenation join_key performs, so every shape
    # refused in a script lands in the key identically when it is written in the prefix. The
    # parametrize is the same list as the script one for that reason: a guard covering one
    # operand of a join is the fault this pair exists to keep closed.
    (tmp_path / 'copy.py').write_text('x')
    pipeline = PipelineConfig.model_validate(
        {
            'resolve_paths_from': str(tmp_path),
            'glue_jobs': {
                'j': {'name': 'n', 'script_bucket': 'sales-scripts', 'script_prefix': written, 'scripts': ['copy.py'], 'role': 'r'}
            },
        }
    )

    faults = pipeline_value_faults('proj', pipeline, on_disk=True)

    assert [(f.site.field, f.fault) for f in faults] == [('script_prefix', expected)]


# Names S3 accepts and names it refuses, written from the service's documentation rather than
# from the code. This is the oracle: `bucket_fault` is one expression and the fake calls it, so
# nothing in the repo can disagree with itself about the rule — what catches the rule being
# wrong is a table that was not derived from it.
BUCKET_NAMES = [
    ('sales-scripts', True),
    ('a1b', True),
    ('my.bucket.name', True),
    ('b', False),  # under the three-character minimum
    ('My_Bucket', False),  # uppercase and underscore
    ('-leading-hyphen', False),
    ('trailing-hyphen-', False),
    ('two..dots', False),
    ('192.168.5.4', False),  # an IP address is reserved
    ('b' * 64, False),  # over the sixty-three character maximum
    ('bucket-{env}', False),  # braces, so a token that never got substituted is caught too
    # Affixes S3 reserves for its own addressing forms. A bucket cannot be created with any of
    # them, so a check that accepts one sends a deploy to a name that can never exist.
    ('xn--example-bucket', False),
    ('sthree-example', False),
    ('example--ol-s3', False),
    ('example-s3alias', False),
    ('amzn-s3-demo-example', False),
    ('example--x-s3', False),
]
# Every row but the templated one, which never reaches the walk as written: `declared_names`
# substitutes first, so the walk sees the name that actually goes to S3.
RESOLVED_BUCKET_NAMES = [row for row in BUCKET_NAMES if '{env}' not in row[0]]


@pytest.mark.parametrize(('written', 'accepted'), RESOLVED_BUCKET_NAMES)
def test_a_bucket_name_s3_would_refuse_is_refused(tmp_path, written, accepted):
    # join_uri joins three strings and key_fault covers two of them. The third reached the
    # network before anything refused it, so `config validate` called the config valid.
    (tmp_path / 'copy.py').write_text('x')
    pipeline = PipelineConfig.model_validate(
        {
            'resolve_paths_from': str(tmp_path),
            'glue_jobs': {'j': {'name': 'n', 'script_bucket': written, 'scripts': ['copy.py'], 'role': 'r'}},
        }
    )

    faults = pipeline_value_faults('proj', pipeline, on_disk=True)

    expected = [] if accepted else [('script_bucket', ConfigFault.NOT_A_BUCKET_NAME)]
    assert [(f.site.field, f.fault) for f in faults] == expected


def test_the_fake_refuses_every_name_the_table_says_s3_refuses():
    # The fake calls `bucket_fault`, so this asserts the shared rule against the table rather
    # than two implementations against each other. What it catches is the rule being wrong: a
    # deploy test uploading to a name S3 would refuse goes green unless the fake refuses it too.
    s3 = FakeS3Client()
    for written, accepted in BUCKET_NAMES:
        fake_accepts = True
        try:
            s3.upload_file(Filename=__file__, Bucket=written, Key='k')
        except ValueError:
            fake_accepts = False
        assert fake_accepts == accepted, f'the fake disagrees with S3 on {written!r}'
        assert (bucket_fault(written) is None) == accepted, f'bucket_fault disagrees with S3 on {written!r}'


@pytest.mark.parametrize(('template', 'accepted'), [('sales-{env}', True), ('Sales-{env}', False)])
def test_the_bucket_checked_is_the_one_the_env_resolves_to(tmp_path, monkeypatch, template, accepted):
    # The name that goes to S3 is the substituted one, so that is the name the check reads. A
    # check reading the template would refuse every braced name and pass every bad resolution.
    monkeypatch.setattr(active_environment, 'name', 'dev')
    (tmp_path / 'copy.py').write_text('x')
    pipeline = PipelineConfig.model_validate(
        {
            'resolve_paths_from': str(tmp_path),
            'glue_jobs': {'j': {'name': 'n', 'script_bucket': template, 'scripts': ['copy.py'], 'role': 'r'}},
        }
    )

    faults = pipeline_value_faults('proj', pipeline, on_disk=True)

    assert (faults == []) == accepted
    assert [f.configured for f in faults] == ([] if accepted else ['Sales-dev'])


def test_every_configured_bucket_is_checked_not_only_the_glue_one(tmp_path):
    # `buckets` entries reach S3 through `s3 export`, `mount` and `uri` rather than through a
    # glue deploy, and they are bucket names by the same rule. Guarding the operand the defect
    # was found in and leaving the sibling is the shape four review rounds kept finding.
    pipeline = PipelineConfig.model_validate({'resolve_paths_from': str(tmp_path), 'buckets': {'raw': 'My_Bucket'}})

    faults = pipeline_value_faults('proj', pipeline, on_disk=True)

    assert [(f.site.resource, f.site.alias, f.site.field, f.fault) for f in faults] == [
        ('s3', 'raw', 'buckets', ConfigFault.NOT_A_BUCKET_NAME)
    ]


def test_a_key_fault_reports_the_string_as_written(tmp_path):
    # Resolution collapses the `.`, the doubled separator and the trailing slash, so reporting
    # the resolved path shows a string that is not malformed and renders three different
    # entries identically.
    pipeline = glue_pipeline(str(tmp_path), ['./jobs/x.py', 'jobs//x.py', 'jobs/x.py/'])

    # Quoted, because the characters are the finding: bare, the trailing slash sits at the end
    # of a sentence with nothing marking it, and an empty prefix shows as nothing at all.
    shown = [f.shown for f in pipeline_value_faults('proj', pipeline, on_disk=True)]

    assert shown == ["'./jobs/x.py'", "'jobs//x.py'", "'jobs/x.py/'"]


@pytest.mark.parametrize('written', ['.', ''])
def test_a_prefix_that_normalizes_to_itself_is_still_refused(tmp_path, written):
    # The round trip against PurePosixPath agreed with itself on these two: `str(PurePosixPath('.'))`
    # is `'.'`, so a bare dot passed the comparison while join_uri built `s3://b/./jobs/copy.py`.
    (tmp_path / 'copy.py').write_text('x')
    pipeline = PipelineConfig.model_validate(
        {
            'resolve_paths_from': str(tmp_path),
            'glue_jobs': {
                'j': {'name': 'n', 'script_bucket': 'sales-scripts', 'script_prefix': written, 'scripts': ['copy.py'], 'role': 'r'}
            },
        }
    )

    faults = pipeline_value_faults('proj', pipeline, on_disk=True)

    assert [(f.site.field, f.fault) for f in faults] == [('script_prefix', ConfigFault.NOT_A_CLEAN_KEY)]


def test_a_glue_job_declaring_no_scripts_is_refused_before_the_deploy_indexes_it(tmp_path):
    # `scripts: []` passes the schema and yields no DeclaredPath, so every check driven by that
    # enumeration had nothing to look at while build_job_update reached scripts[0]. Both doors
    # name it now: validate exits 1 rather than 0, and the deploy refuses rather than raising.
    pipeline = glue_pipeline(str(tmp_path), [])

    faults = pipeline_value_faults('proj', pipeline, on_disk=True)

    assert [(f.site.field, f.fault) for f in faults] == [('scripts', ConfigFault.DECLARES_NOTHING)]
    with pytest.raises(typer.Exit):
        resolve_scripts('proj', pipeline, 'j')


def test_a_malformed_script_is_not_also_reported_as_missing(tmp_path):
    # Two rows for one entry, and the second asks the wrong question: `./jobs/x.py` resolves to
    # somewhere real-looking, so `not found` reads as the remedy when the remedy is the spelling.
    # Its sibling still gets the on-disk check, which is what keys the suppression on the value.
    (tmp_path / 'jobs').mkdir()
    pipeline = glue_pipeline(str(tmp_path), ['./jobs/x.py', 'jobs/present.py'])

    faults = pipeline_value_faults('proj', pipeline, on_disk=True)

    assert [(f.configured, f.fault) for f in faults] == [
        ('./jobs/x.py', ConfigFault.NOT_A_CLEAN_KEY),
        ('jobs/present.py', ConfigFault.ABSENT),
    ]


def test_an_absent_root_is_reported_alone_at_the_deploy_door(tmp_path):
    # The deploy door is the one you are at when it matters. Filtering a shared diagnoser's
    # result to one job drops the root row, and ten scripts then print ten lines that each name
    # a file while the cause — no checkout — is named in none of them. Asserted on the rows the
    # door reports, not on what they render to.
    pipeline = glue_pipeline(str(tmp_path / 'nowhere'), ['jobs/a.py', 'jobs/b.py'])

    scoped = pipeline_value_faults('proj', pipeline, on_disk=True, resource='glue', alias='j')

    assert [(f.site, f.fault) for f in scoped] == [(ROOT_SITE, ConfigFault.ABSENT)]
    with pytest.raises(typer.Exit):
        resolve_scripts('proj', pipeline, 'j')


@pytest.mark.parametrize(('resource', 'alias'), [('glue_jobs', 'j'), ('glue', 'J')])
def test_a_scope_naming_nothing_raises_rather_than_reporting_a_clean_config(tmp_path, resource, alias):
    # An unrecognized scope returned no faults, and every door reads no faults as a clean config
    # and proceeds to deploy. `glue_jobs` is the collection where `glue` is the resource, and
    # both spellings live in the same module, so the typo is the one a reader would make.
    pipeline = glue_pipeline(str(tmp_path / 'nowhere'), ['jobs/a.py'])

    with pytest.raises(ValueError):
        pipeline_value_faults('proj', pipeline, on_disk=True, resource=resource, alias=alias)


def test_both_doors_report_one_absent_root_the_same_way(tmp_path):
    # The property `pipeline_value_faults`'s docstring claims. `config validate` walks the whole
    # config and a deploy scopes to one job; scoping is an argument, so neither door can filter
    # the row that carries the cause out of its own answer.
    pipeline = glue_pipeline(str(tmp_path / 'nowhere'), ['jobs/a.py'])
    config = DectlConfig.model_validate({'defaults': {'account_id': '1'}, 'pipelines': {'proj': pipeline.model_dump()}})

    from_validate = config_value_faults(config)
    from_deploy = pipeline_value_faults('proj', pipeline, on_disk=True, resource='glue', alias='j')

    assert from_validate == from_deploy


def test_one_missing_script_resolves_none_of_them(tmp_path):
    # A partial upload leaves the job's scripts split across two revisions, and the next run
    # mixes them, so the whole set is checked before the first byte goes anywhere.
    (tmp_path / 'present.py').write_text('x')
    pipeline = glue_pipeline(str(tmp_path), ['present.py', 'absent.py'])

    with pytest.raises(typer.Exit) as exit_info:
        resolve_scripts('proj', pipeline, 'j')

    assert exit_info.value.exit_code == 1


def test_every_site_composing_a_script_destination_substitutes_exactly_once(tmp_path, monkeypatch):
    """The upload, both job-definition fields, the renderers' row and the help panel, together.

    Substituting twice agrees with substituting once for every environment name that does not
    itself carry the token, so the two conventions are indistinguishable on ordinary input.
    `a{env}b` tells them apart, and driving one site with it leaves the others free to disagree
    — which is what happened: a test pinned the key and the renderers while `upload_scripts`,
    the site that writes, rendered a job the caller had rendered.

    The bucket is held by identity rather than by discrimination, and the reason is a limit
    worth knowing before trusting the name. Telling one substitution from two needs an
    environment whose own name carries the token, and every spelling of a `{env}`-bearing
    bucket under such a name carries a brace — which `bucket_fault` refuses, so `config
    validate` exits before any deploy reaches here. No valid config can double-substitute a
    bucket. What is left to hold is that the upload sends the job's own field rather than
    anything derived from it, and that is what the first assertion says."""
    monkeypatch.setattr(active_environment, 'name', 'a{env}b')
    (tmp_path / 'p-a{env}b').mkdir()
    for leaf in ('main-a{env}b.py', 'util-a{env}b.py'):
        (tmp_path / 'p-a{env}b' / leaf).write_text('x')
    pipeline = PipelineConfig.model_validate(
        {
            'resolve_paths_from': str(tmp_path),
            'glue_jobs': {
                'j': {
                    'name': 'my-job',
                    'script_bucket': 'salesdata-scripts',
                    'script_prefix': 'k-{env}',
                    'scripts': ['p-{env}/main-{env}.py', 'p-{env}/util-{env}.py'],
                    'role': 'r',
                }
            },
        }
    )
    job = render_env_model(pipeline.glue_jobs['j'])
    s3 = FakeS3Client()

    upload_scripts(FakeS3Session(s3), job, resolve_scripts('proj', pipeline, 'j'))
    definition = build_job_update({'Name': 'my-job'}, job)

    once = 'p-a{env}b/main-a{env}b.py'
    assert s3.uploads[0][1] == job.script_bucket
    assert s3.uploads[0][2] == f'k-a{{env}}b/{once}'
    assert definition['Command']['ScriptLocation'] == f's3://salesdata-scripts/k-a{{env}}b/{once}'
    assert definition['DefaultArguments']['--extra-py-files'] == f's3://{s3.uploads[1][1]}/{s3.uploads[1][2]}'
    assert script_uris(pipeline.glue_jobs['j'])[0] == f's3://salesdata-scripts/k-a{{env}}b/{once}'
    # The help panel is the one site that renders nothing, because it is built before `--env`
    # is parsed. Its operands are raw, so the token stands rather than being resolved wrongly.
    assert configured_uris(pipeline.glue_jobs['j']).startswith('s3://salesdata-scripts/k-{env}/p-{env}/main-{env}.py')


def test_upload_scripts_refuses_a_partial_set_handed_to_it_directly(tmp_path):
    # The invariant belongs to the function, not to whoever calls it. Every live caller
    # resolves first; this one does not, and the promise has to hold anyway.
    (tmp_path / 'present.py').write_text('x')
    pipeline = glue_pipeline(str(tmp_path), ['present.py'])
    s3 = FakeS3Client()
    sources = [ResolvedScript('present.py', tmp_path / 'present.py'), ResolvedScript('absent.py', tmp_path / 'absent.py')]

    with pytest.raises(typer.Exit):
        upload_scripts(FakeS3Session(s3), pipeline.glue_jobs['j'], sources)

    assert s3.uploads == []


def test_a_directory_named_as_a_script_is_reported_as_a_directory(tmp_path):
    # Present with the wrong type and absent have opposite remedies — fix the config key, or
    # check the tree out — so they are separate values rather than one wording apart. Asserted
    # on the value: rich soft-wraps the sentence at the width whoever ran the suite happened to
    # have, and a helper that normalizes the wrapping is a width pin by another name.
    (tmp_path / 'copy.py').mkdir()
    pipeline = glue_pipeline(str(tmp_path), ['copy.py'])

    faults = pipeline_value_faults('proj', pipeline, on_disk=True)

    assert [f.fault for f in faults] == [ConfigFault.EXPECTED_FILE]
    with pytest.raises(typer.Exit):
        resolve_scripts('proj', pipeline, 'j')


def test_the_deploy_door_names_the_pipeline_and_the_config_key(tmp_path, capsys):
    # config validate names both. The deploy is the door you are at when it matters, so it
    # cannot say less.
    pipeline = glue_pipeline(str(tmp_path), ['jobs/copy.py'])

    with pytest.raises(typer.Exit):
        resolve_scripts('salesdata', pipeline, 'j')

    # Asserted on the row rather than on the sentence: `'glue/j script'` is a prefix of
    # `'glue/j script_prefix'`, so a substring match cannot say which key the door named.
    faults = pipeline_value_faults('salesdata', pipeline, on_disk=True, resource='glue', alias='j')
    assert [(f.pipeline, f.site) for f in faults] == [('salesdata', ValueSite('glue', 'j', 'scripts'))]
    assert 'dectl config show' in capsys.readouterr().err


class FakeDeploySession:
    """Serves both clients one deploy needs, and records what each was asked to do."""

    def __init__(self, existing_job):
        self.glue = FakeGlueClient(existing_job)
        self.s3 = FakeS3Client()

    def client(self, name):
        return self.s3 if name == 's3' else self.glue


def deploy_config(root, max_capacity=None) -> DectlConfig:
    # A real bucket name: FakeS3Client applies S3's own naming rule, and every deploy test here
    # but one refuses before reaching the upload, so a rejected name would go unnoticed.
    job = {'name': 'my-job', 'script_bucket': 'salesdata-scripts', 'scripts': ['copy.py'], 'role': 'new-role'}
    if max_capacity is not None:
        job['max_capacity'] = max_capacity
    return DectlConfig.model_validate(
        {'defaults': {'account_id': '1'}, 'pipelines': {'proj': {'resolve_paths_from': str(root), 'glue_jobs': {'copy': job}}}}
    )


def test_a_refused_definition_change_uploads_nothing(monkeypatch, tmp_path):
    # build_job_update exits on a max_capacity Glue would reject. Uploading first leaves
    # ScriptLocation pointing at code the user is then told not to deploy.
    (tmp_path / 'copy.py').write_text('x')
    config = deploy_config(tmp_path, max_capacity=1)
    session = FakeDeploySession({'Name': 'my-job', 'Role': 'old-role', 'WorkerType': 'G.1X', 'NumberOfWorkers': 2})
    monkeypatch.setattr('dectl.session.make_session', lambda _config: session)

    result = runner.invoke(make_glue_app('proj', config.pipelines['proj'], config), ['copy', 'deploy', '--yes'])

    assert result.exit_code == 1
    assert session.s3.uploads == []
    assert session.glue.captured_update is None


def test_a_refused_confirmation_uploads_nothing(monkeypatch, tmp_path):
    # Without --yes and with no terminal, confirm_or_exit refuses rather than blocking. The
    # deploy stops there, and the point is that it stops before the upload rather than after.
    (tmp_path / 'copy.py').write_text('x')
    config = deploy_config(tmp_path)
    session = FakeDeploySession({'Name': 'my-job', 'Role': 'old-role'})
    monkeypatch.setattr('dectl.session.make_session', lambda _config: session)

    result = runner.invoke(make_glue_app('proj', config.pipelines['proj'], config), ['copy', 'deploy'])

    assert result.exit_code == 1
    assert session.s3.uploads == []
    assert session.glue.captured_update is None


def test_plan_refuses_a_missing_script_rather_than_reporting_a_clean_diff(monkeypatch, tmp_path):
    # --plan is the rehearsal. Reporting a clean definition for a config whose real deploy exits
    # makes the one command run to answer "will this work" the one that cannot.
    config = deploy_config(tmp_path)
    session = FakeDeploySession({'Name': 'my-job', 'Role': 'old-role'})
    monkeypatch.setattr('dectl.session.make_session', lambda _config: session)

    result = runner.invoke(make_glue_app('proj', config.pipelines['proj'], config), ['copy', 'deploy', '--plan'])

    assert result.exit_code == 1
    assert session.s3.uploads == []


def test_deploy_handles_a_script_whose_name_carries_the_env_token(monkeypatch, tmp_path):
    # The verb holds a render_env_model'd job, so both sides of the set guard have to be
    # substituted names: comparing 'jobs/{env}/copy.py' to 'jobs/prod/copy.py' presents two
    # spellings of one file as two files. Driven through the verb, because handing
    # upload_scripts a raw pipeline.glue_jobs['j'] hides exactly that mismatch.
    monkeypatch.setattr(active_environment, 'name', 'prod')
    (tmp_path / 'jobs' / 'prod').mkdir(parents=True)
    (tmp_path / 'jobs' / 'prod' / 'copy.py').write_text('x')
    config = DectlConfig.model_validate(
        {
            'defaults': {'account_id': '1'},
            'pipelines': {
                'proj': {
                    'resolve_paths_from': str(tmp_path),
                    'glue_jobs': {
                        'copy': {
                            'name': 'my-job',
                            'script_bucket': 'salesdata-scripts',
                            'scripts': ['jobs/{env}/copy.py'],
                            'role': 'new-role',
                        }
                    },
                }
            },
        }
    )
    session = FakeDeploySession({'Name': 'my-job', 'Role': 'old-role'})
    monkeypatch.setattr('dectl.session.make_session', lambda _config: session)

    result = runner.invoke(make_glue_app('proj', config.pipelines['proj'], config), ['copy', 'deploy', '--yes'])

    assert result.exit_code == 0
    assert [key for _, _, key in session.s3.uploads] == ['scripts/jobs/prod/copy.py']
    assert session.glue.captured_update['Command']['ScriptLocation'] == 's3://salesdata-scripts/scripts/jobs/prod/copy.py'


def test_deploy_uploads_every_script_and_writes_the_definition(monkeypatch, tmp_path):
    # The write path, end to end. Nothing else asserts the writes happen at all, so deleting
    # either the upload call or the apply call from the deploy verb is invisible without this.
    (tmp_path / 'copy.py').write_text('x')
    config = deploy_config(tmp_path)
    session = FakeDeploySession({'Name': 'my-job', 'Role': 'old-role'})
    monkeypatch.setattr('dectl.session.make_session', lambda _config: session)

    result = runner.invoke(make_glue_app('proj', config.pipelines['proj'], config), ['copy', 'deploy', '--yes'])

    assert result.exit_code == 0
    assert [(bucket, key) for _, bucket, key in session.s3.uploads] == [('salesdata-scripts', 'scripts/copy.py')]
    assert session.glue.captured_update['Role'] == 'new-role'
    assert session.glue.captured_update['Command']['ScriptLocation'] == 's3://salesdata-scripts/scripts/copy.py'


def test_uploading_a_set_the_definition_does_not_name_is_refused(tmp_path):
    # Taking `sources` from the caller makes the set uploaded breakable against the set the
    # definition names. Every path here exists, so nothing else in the function refuses, and
    # the deploy would write a ScriptLocation naming an object nothing uploaded.
    (tmp_path / 'first.py').write_text('x')
    job = GlueJobConfig(name='my-job', script_bucket='sales-scripts', scripts=['first.py', 'second.py'], role='r')
    s3 = FakeS3Client()

    with pytest.raises(typer.Exit):
        upload_scripts(FakeS3Session(s3), job, [ResolvedScript('first.py', tmp_path / 'first.py')])

    assert s3.uploads == []
