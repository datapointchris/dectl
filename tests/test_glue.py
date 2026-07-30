import pytest
import typer
from typer.testing import CliRunner

from dectl.commands.glue import build_job_update
from dectl.commands.glue import job_definition_changes
from dectl.commands.glue import make_glue_app
from dectl.commands.glue import update_glue_job
from dectl.config import DectlConfig
from dectl.config import GlueJobConfig

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
    update_glue_job(FakeSession(glue), job, assume_yes=True)


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
    update_glue_job(FakeSession(glue), job)

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
