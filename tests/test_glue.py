from pathlib import Path

import pytest
import typer

from dectl import prompt
from dectl.commands.glue import apply_glue_job_update
from dectl.commands.glue import build_job_update
from dectl.commands.glue import confirm_glue_job_update
from dectl.commands.glue import follow_glue_run
from dectl.commands.glue import job_definition_changes
from dectl.commands.glue import make_glue_app
from dectl.commands.glue import resolve_scripts
from dectl.commands.glue import upload_scripts
from dectl.config import DectlConfig
from dectl.config import GlueJobConfig
from dectl.config import PipelineConfig
from dectl.config import pipeline_value_faults
from dectl.values import ConfigFault
from dectl.values import bucket_fault
from tests.conftest import RefusalRunner

runner = RefusalRunner()


class FakeGlueClient:
    """Glue stand-in that refuses the UpdateJob this whole change is built around.

    A job sizes by DPU or by workers, and `UpdateJob` rejects a definition carrying each. Every
    assertion here about suppressing a derived `MaxCapacity` rests on that, so a fake that
    accepts both makes a build which forgot to drop one look identical to a build that dropped
    it — and the difference is a deploy that fails after the scripts are already uploaded."""

    def __init__(self, existing_job):
        self.existing_job = existing_job
        self.captured_update = None

    def get_job(self, JobName):
        return {'Job': self.existing_job}

    def update_job(self, JobName, JobUpdate):
        worker_keys = sorted(key for key in ('WorkerType', 'NumberOfWorkers') if key in JobUpdate)
        if worker_keys and 'MaxCapacity' in JobUpdate:
            raise AssertionError(f'UpdateJob carries MaxCapacity beside {" and ".join(worker_keys)}; Glue refuses a job sized both ways')
        self.captured_update = JobUpdate
        return {}


class FakeSession:
    def __init__(self, glue_client):
        self.glue_client = glue_client

    def client(self, name):
        assert name == 'glue'
        return self.glue_client


def make_job(**overrides):
    """One job with any field overridden by name.

    Keyword-wise rather than a fixed parameter list, because every managed field has a model
    default and a test that spells out the ones it is not about hides the one it is."""
    return GlueJobConfig(
        name='my-job',
        script_bucket='my-bucket',
        scripts=['main.py'],
        role='arn:aws:iam::123456789012:role/glue',
        **overrides,
    )


def apply(glue, job):
    """The sequence `deploy` runs, so these tests exercise the path the CLI takes."""
    session = FakeSession(glue)
    job_update = confirm_glue_job_update(session, job, assume_yes=True)
    if job_update is not None:
        apply_glue_job_update(session, job, job_update)


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


def test_worker_sizing_on_a_python_shell_job_is_rejected():
    # The mirror of the case above, and the same cost it exists to avoid: UpdateJob refuses the
    # pair on a pythonshell command, after upload_scripts has already run.
    glue = FakeGlueClient(existing_job={'Command': {'Name': 'pythonshell'}, 'MaxCapacity': 1.0})

    with pytest.raises(typer.Exit):
        apply(glue, make_job(worker_type='G.1X', number_of_workers=2))

    assert glue.captured_update is None


def worker_based_job(**definition):
    """A Spark job as GetJob returns one: the worker pair, plus the MaxCapacity Glue derives.

    The derived value is the whole point. A fixture omitting it describes a job Glue does not
    serve, and every assertion about suppressing it would pass against nothing."""
    return {
        'Command': {'Name': 'glueetl', 'ScriptLocation': 's3://my-bucket/scripts/main.py'},
        'Role': 'arn:aws:iam::123456789012:role/glue',
        'DefaultArguments': {'--JOB_NAME': 'my-job'},
        'GlueVersion': '4.0',
        'WorkerType': 'G.1X',
        'NumberOfWorkers': 4,
        'MaxCapacity': 4.0,
        **definition,
    }


def test_a_derived_max_capacity_is_not_reported_as_a_change():
    # UpdateJob rejects MaxCapacity beside the worker pair, so dropping it is forced. Reported
    # as a removal it is a row the next GetJob puts straight back, so the job would prompt on
    # every deploy and never reach the unchanged-definition steady state.
    existing = worker_based_job()

    changes = job_definition_changes(existing, build_job_update(existing, make_job()))

    assert changes == []


def test_worker_sizing_displaces_a_configured_max_capacity_and_says_so():
    # The opposite case to the one above: the config asked for this removal, so it is a change
    # the reader confirms rather than one suppressed as forced.
    existing = {
        'Command': {'Name': 'glueetl', 'ScriptLocation': 's3://my-bucket/scripts/main.py'},
        'Role': 'arn:aws:iam::123456789012:role/glue',
        'DefaultArguments': {'--JOB_NAME': 'my-job'},
        'MaxCapacity': 10.0,
    }
    job = make_job(worker_type='G.2X', number_of_workers=4)

    update = build_job_update(existing, job)
    changes = job_definition_changes(existing, update)

    assert 'MaxCapacity' not in update.definition
    assert ('MaxCapacity', '10.0', '(removed)') in changes
    assert ('WorkerType', '(unset)', 'G.2X') in changes
    assert ('NumberOfWorkers', '(unset)', '4') in changes


def test_a_new_script_location_is_reported_and_applied():
    existing = {
        'Command': {'Name': 'pythonshell', 'ScriptLocation': 's3://old-bucket/scripts/old.py'},
        'Role': 'arn:aws:iam::123456789012:role/glue',
        'DefaultArguments': {'--JOB_NAME': 'my-job'},
    }
    glue = FakeGlueClient(existing_job=existing)

    changes = job_definition_changes(dict(existing), build_job_update(dict(existing), make_job()))
    apply(glue, make_job())

    assert ('Command.ScriptLocation', 's3://old-bucket/scripts/old.py', 's3://my-bucket/scripts/main.py') in changes
    assert glue.captured_update['Command']['ScriptLocation'] == 's3://my-bucket/scripts/main.py'


# One row per managed field: the live job it runs against, the config that names it, and what
# the update must then carry. Every field in one table rather than a table plus bespoke tests,
# because the guard below is only as good as what a deleted test takes with it — a field whose
# coverage lives in a function of its own can lose that function while an inline allow-list keeps
# the guard green. Delete a row here and the field loses its drive and its claim together.
SPARK = {'Command': {'Name': 'glueetl'}}
SHELL = {'Command': {'Name': 'pythonshell', 'PythonVersion': '3.9'}}
MANAGED_FIELD_SPECS = [
    pytest.param(SPARK, {'glue_version': '5.0'}, lambda u: u['GlueVersion'] == '5.0', id='glue_version'),
    pytest.param(SPARK, {'timeout_minutes': 15}, lambda u: u['Timeout'] == 15, id='timeout_minutes'),
    pytest.param(SPARK, {'max_retries': 0}, lambda u: u['MaxRetries'] == 0, id='max_retries'),
    pytest.param(SPARK, {'execution_class': 'FLEX'}, lambda u: u['ExecutionClass'] == 'FLEX', id='execution_class'),
    pytest.param(
        SPARK,
        {'worker_type': 'G.2X', 'number_of_workers': 3},
        lambda u: u['WorkerType'] == 'G.2X' and u['NumberOfWorkers'] == 3,
        id='worker_type+number_of_workers',
    ),
    pytest.param(SHELL, {'max_capacity': 1.0}, lambda u: u['MaxCapacity'] == 1.0, id='max_capacity'),
    # The two nested ones assert the sibling survives as well, because a merge that replaced the
    # sub-dict rather than updating it would satisfy the key on its own.
    pytest.param(
        SHELL,
        {'python_version': '3.11'},
        lambda u: u['Command']['PythonVersion'] == '3.11' and u['Command']['Name'] == 'pythonshell',
        id='python_version',
    ),
    pytest.param(
        {**SPARK, 'ExecutionProperty': {'MaxConcurrentRuns': 1}},
        {'max_concurrent_runs': 3},
        lambda u: u['ExecutionProperty'] == {'MaxConcurrentRuns': 3},
        id='max_concurrent_runs',
    ),
]


@pytest.mark.parametrize('existing, configured, holds', MANAGED_FIELD_SPECS)
def test_a_configured_definition_field_reaches_the_update(existing, configured, holds):
    glue = FakeGlueClient(existing_job=dict(existing))

    apply(glue, make_job(**configured))

    assert holds(glue.captured_update)


def test_no_input_refuses_the_prompt_even_on_a_terminal(monkeypatch):
    monkeypatch.setattr('sys.stdin.isatty', lambda: True)
    monkeypatch.setattr(prompt.interactivity, 'no_input', True)
    glue = FakeGlueClient(existing_job={'Command': {'Name': 'pythonshell'}})

    with pytest.raises(typer.Exit):
        confirm_glue_job_update(FakeSession(glue), make_job())

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
        ('../scripts', ConfigFault.KEY_ESCAPES_ROOT),
        ('./scripts', ConfigFault.NOT_A_CLEAN_KEY),
    ],
)
def test_a_script_prefix_is_refused_like_a_script(tmp_path, written, expected):
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


# Written from S3's documentation rather than from `bucket_fault`, so the rule is checked against something it was not derived from.
BUCKET_NAMES = [
    ('sales-scripts', True),
    ('my.bucket.name', True),
    ('b', False),
    ('My_Bucket', False),
    ('192.168.5.4', False),
    ('example--x-s3', False),
]


@pytest.mark.parametrize(('written', 'accepted'), BUCKET_NAMES)
def test_a_bucket_name_s3_would_refuse_is_refused(tmp_path, written, accepted):
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


def test_a_glue_job_declaring_no_scripts_is_refused_before_the_deploy_indexes_it(tmp_path):
    # `scripts: []` passes the schema and yields no DeclaredPath, so every check driven by that
    # enumeration had nothing to look at while build_job_update reached scripts[0]. Both doors
    # name it now: validate exits 1 rather than 0, and the deploy refuses rather than raising.
    pipeline = glue_pipeline(str(tmp_path), [])

    faults = pipeline_value_faults('proj', pipeline, on_disk=True)

    assert [(f.site.field, f.fault) for f in faults] == [('scripts', ConfigFault.DECLARES_NOTHING)]
    with pytest.raises(typer.Exit):
        resolve_scripts('proj', pipeline, 'j')


def test_one_missing_script_resolves_none_of_them(tmp_path):
    # A partial upload leaves the job's scripts split across two revisions, and the next run
    # mixes them, so the whole set is checked before the first byte goes anywhere.
    (tmp_path / 'present.py').write_text('x')
    pipeline = glue_pipeline(str(tmp_path), ['present.py', 'absent.py'])

    with pytest.raises(typer.Exit) as exit_info:
        resolve_scripts('proj', pipeline, 'j')

    assert exit_info.value.exit_code == 1


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
    monkeypatch.setattr('dectl.commands.glue.make_session', lambda _config: session)

    result = runner.invoke(make_glue_app('proj', config.pipelines['proj'], config), ['copy', 'deploy', '--yes'])

    assert result.exit_code == 1
    assert session.s3.uploads == []
    assert session.glue.captured_update is None


def test_plan_refuses_a_missing_script_rather_than_reporting_a_clean_diff(monkeypatch, tmp_path):
    # plan is the rehearsal. Reporting a clean definition for a config whose real deploy exits
    # makes the one command run to answer "will this work" the one that cannot.
    config = deploy_config(tmp_path)
    session = FakeDeploySession({'Name': 'my-job', 'Role': 'old-role'})
    monkeypatch.setattr('dectl.commands.glue.make_session', lambda _config: session)

    result = runner.invoke(make_glue_app('proj', config.pipelines['proj'], config), ['copy', 'plan'])

    assert result.exit_code == 1
    assert session.s3.uploads == []


def test_deploy_uploads_every_script_and_writes_the_definition(monkeypatch, tmp_path):
    # The write path, end to end. Nothing else asserts the writes happen at all, so deleting
    # either the upload call or the apply call from the deploy verb is invisible without this.
    (tmp_path / 'copy.py').write_text('x')
    config = deploy_config(tmp_path)
    session = FakeDeploySession({'Name': 'my-job', 'Role': 'old-role'})
    monkeypatch.setattr('dectl.commands.glue.make_session', lambda _config: session)

    result = runner.invoke(make_glue_app('proj', config.pipelines['proj'], config), ['copy', 'deploy', '--yes'])

    assert result.exit_code == 0
    assert [(bucket, key) for _, bucket, key in session.s3.uploads] == [('salesdata-scripts', 'scripts/copy.py')]
    assert session.glue.captured_update['Role'] == 'new-role'
    assert session.glue.captured_update['Command']['ScriptLocation'] == 's3://salesdata-scripts/scripts/copy.py'
