import re
from pathlib import Path

import pytest
import typer
from typer.testing import CliRunner

from dectl import prompt
from dectl.commands.glue import GlueRunWatcher
from dectl.commands.glue import apply_glue_job_update
from dectl.commands.glue import build_job_update
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
from dectl.config import config_path_faults
from dectl.config import pipeline_path_faults
from dectl.env import active_environment
from dectl.paths import PathFault

runner = CliRunner()


def test_job_exposes_deploy_run_logs_runs_verbs():
    config = DectlConfig.model_validate(
        {
            'defaults': {'account_id': '123456789012'},
            'pipelines': {'proj': {'glue_jobs': {'source-copy': {'name': 'j', 'script_bucket': 'b', 'scripts': ['s.py'], 'role': 'r'}}}},
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
        # Real S3 answers InvalidBucketName for anything outside this set, so a fake that took
        # any string would let an unsubstituted `b-{env}` look identical to a substituted one.
        bucket = kwargs['Bucket']
        if not re.fullmatch(r'[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]', bucket):
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
    job = pipeline.glue_jobs['j']
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
    job = pipeline.glue_jobs['j']
    s3 = FakeS3Client()

    upload_scripts(FakeS3Session(s3), job, resolve_scripts('proj', pipeline, 'j'))
    definition = build_job_update({'Name': 'my-job'}, job)

    assert definition['DefaultArguments']['--extra-py-files'] == f's3://{s3.uploads[1][1]}/{s3.uploads[1][2]}'


@pytest.mark.parametrize(
    ('written', 'expected'),
    [
        ('/srv/shared/handler.py', PathFault.ESCAPES_ROOT),
        ('../sibling/util.py', PathFault.ESCAPES_ROOT),
        ('~/shared/lib.py', PathFault.ESCAPES_ROOT),
        ('./copy.py', PathFault.NOT_A_CLEAN_KEY),
        ('jobs//copy.py', PathFault.NOT_A_CLEAN_KEY),
        ('jobs/./copy.py', PathFault.NOT_A_CLEAN_KEY),
        ('copy.py/', PathFault.NOT_A_CLEAN_KEY),
    ],
)
def test_a_key_that_s3_would_store_literally_is_refused(tmp_path, written, expected):
    # Each shape is pinned to its own fault, not to "one of the two". `validate --json`
    # publishes the name, so an assertion satisfied by either value pins neither: making
    # NOT_A_CLEAN_KEY unreachable left the suite green.
    #
    # A leading ~ is ESCAPES_ROOT rather than a spelling fault: it resolves through $HOME, which
    # is the same dependence on where the deploy ran that `resolve_paths_from` exists to remove.
    pipeline = glue_pipeline(str(tmp_path), [written])

    assert [f.fault for f in pipeline_path_faults('proj', pipeline)] == [expected]


@pytest.mark.parametrize(
    ('written', 'expected'),
    [
        ('/scripts', PathFault.ESCAPES_ROOT),
        ('../scripts', PathFault.ESCAPES_ROOT),
        ('~/scripts', PathFault.ESCAPES_ROOT),
        ('./scripts', PathFault.NOT_A_CLEAN_KEY),
        ('scripts/', PathFault.NOT_A_CLEAN_KEY),
        ('scripts//sub', PathFault.NOT_A_CLEAN_KEY),
    ],
)
def test_a_script_prefix_is_refused_for_every_shape_a_script_is(tmp_path, written, expected):
    # The prefix is the other operand of the concatenation script_key performs, so every shape
    # refused in a script lands in the key identically when it is written in the prefix. The
    # parametrize is the same list as the script one for that reason: a guard covering one
    # operand of a join is the fault this pair exists to keep closed.
    (tmp_path / 'copy.py').write_text('x')
    pipeline = PipelineConfig.model_validate(
        {
            'resolve_paths_from': str(tmp_path),
            'glue_jobs': {'j': {'name': 'n', 'script_bucket': 'b', 'script_prefix': written, 'scripts': ['copy.py'], 'role': 'r'}},
        }
    )

    faults = pipeline_path_faults('proj', pipeline)

    assert [(f.site.field, f.fault) for f in faults] == [('script_prefix', expected)]


def test_a_key_fault_reports_the_string_as_written(tmp_path):
    # Resolution collapses the `.`, the doubled separator and the trailing slash, so reporting
    # the resolved path shows a string that is not malformed and renders three different
    # entries identically.
    pipeline = glue_pipeline(str(tmp_path), ['./jobs/x.py', 'jobs//x.py', 'jobs/x.py/'])

    shown = [f.shown for f in pipeline_path_faults('proj', pipeline)]

    assert shown == ['./jobs/x.py', 'jobs//x.py', 'jobs/x.py/']


def test_a_malformed_script_is_not_also_reported_as_missing(tmp_path):
    # Two rows for one entry, and the second asks the wrong question: `./jobs/x.py` resolves to
    # somewhere real-looking, so `not found` reads as the remedy when the remedy is the spelling.
    # Its sibling still gets the on-disk check, which is what keys the suppression on the value.
    (tmp_path / 'jobs').mkdir()
    pipeline = glue_pipeline(str(tmp_path), ['./jobs/x.py', 'jobs/present.py'])

    faults = pipeline_path_faults('proj', pipeline)

    assert [(f.configured, f.fault) for f in faults] == [
        ('./jobs/x.py', PathFault.NOT_A_CLEAN_KEY),
        ('jobs/present.py', PathFault.ABSENT),
    ]


def test_an_absent_root_is_reported_alone_at_the_deploy_door(tmp_path):
    # The deploy door is the one you are at when it matters. Filtering a shared diagnoser's
    # result to one job drops the root row, and ten scripts then print ten lines that each name
    # a file while the cause — no checkout — is named in none of them. Asserted on the rows the
    # door reports, not on what they render to.
    pipeline = glue_pipeline(str(tmp_path / 'nowhere'), ['jobs/a.py', 'jobs/b.py'])

    scoped = pipeline_path_faults('proj', pipeline, resource='glue', alias='j')

    assert [(f.site, f.fault) for f in scoped] == [(ROOT_SITE, PathFault.ABSENT)]
    with pytest.raises(typer.Exit):
        resolve_scripts('proj', pipeline, 'j')


def test_both_doors_report_one_absent_root_the_same_way(tmp_path):
    # The property `pipeline_path_faults`'s docstring claims. `config validate` walks the whole
    # config and a deploy scopes to one job; scoping is an argument, so neither door can filter
    # the row that carries the cause out of its own answer.
    pipeline = glue_pipeline(str(tmp_path / 'nowhere'), ['jobs/a.py'])
    config = DectlConfig.model_validate({'defaults': {'account_id': '1'}, 'pipelines': {'proj': pipeline.model_dump()}})

    from_validate = config_path_faults(config)
    from_deploy = pipeline_path_faults('proj', pipeline, resource='glue', alias='j')

    assert from_validate == from_deploy


def test_a_malformed_script_still_loads_so_the_rest_of_the_cli_survives(tmp_path):
    # A schema failure blanks every pipeline command, including the ones that would report the
    # problem. The config loads and the deploy refuses instead.
    pipeline = glue_pipeline(str(tmp_path), ['/srv/shared/handler.py'])

    assert pipeline.glue_jobs['j'].scripts == ['/srv/shared/handler.py']


def test_one_missing_script_resolves_none_of_them(tmp_path):
    # A partial upload leaves the job's scripts split across two revisions, and the next run
    # mixes them, so the whole set is checked before the first byte goes anywhere.
    (tmp_path / 'present.py').write_text('x')
    pipeline = glue_pipeline(str(tmp_path), ['present.py', 'absent.py'])

    with pytest.raises(typer.Exit) as exit_info:
        resolve_scripts('proj', pipeline, 'j')

    assert exit_info.value.exit_code == 1


def test_upload_scripts_refuses_a_partial_set_handed_to_it_directly(tmp_path):
    # The invariant belongs to the function, not to whoever calls it. Every live caller
    # resolves first; this one does not, and the promise has to hold anyway.
    (tmp_path / 'present.py').write_text('x')
    pipeline = glue_pipeline(str(tmp_path), ['present.py'])
    s3 = FakeS3Client()
    sources = [('present.py', tmp_path / 'present.py'), ('absent.py', tmp_path / 'absent.py')]

    with pytest.raises(typer.Exit):
        upload_scripts(FakeS3Session(s3), pipeline.glue_jobs['j'], sources)

    assert s3.uploads == []


def test_a_directory_named_as_a_script_is_reported_as_a_directory(tmp_path, capsys):
    # Present with the wrong type and absent have opposite remedies, so they never share a
    # wording: fix the config key, or check the tree out.
    (tmp_path / 'copy.py').mkdir()
    pipeline = glue_pipeline(str(tmp_path), ['copy.py'])

    with pytest.raises(typer.Exit):
        resolve_scripts('proj', pipeline, 'j')

    assert 'is a directory, not a file' in capsys.readouterr().err


def test_the_deploy_door_names_the_pipeline_and_the_config_key(tmp_path, capsys):
    # config validate names both. The deploy is the door you are at when it matters, so it
    # cannot say less.
    pipeline = glue_pipeline(str(tmp_path), ['jobs/copy.py'])

    with pytest.raises(typer.Exit):
        resolve_scripts('salesdata', pipeline, 'j')

    err = capsys.readouterr().err
    assert 'salesdata' in err
    assert 'glue/j script' in err
    assert 'dectl config show' in err


class FakeDeploySession:
    """Serves both clients one deploy needs, and records what each was asked to do."""

    def __init__(self, existing_job):
        self.glue = FakeGlueClient(existing_job)
        self.s3 = FakeS3Client()

    def client(self, name):
        return self.s3 if name == 's3' else self.glue


def deploy_config(root, max_capacity=None) -> DectlConfig:
    job = {'name': 'my-job', 'script_bucket': 'b', 'scripts': ['copy.py'], 'role': 'new-role'}
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
