"""Live AWS round-trip test for the Glue deploy path.

Opt-in only: run with `uv run pytest --run-integration`. It creates a throwaway IAM
role and Glue job in the caller's AWS account, runs dectl's deploy against it, asserts
the job definition round-trips, then deletes everything. Glue create/update/get/delete
are free -- the test never starts a job run, so it costs nothing.

Region/profile come from the standard AWS environment (AWS_PROFILE, AWS_REGION), with
DECTL_IT_AWS_PROFILE / DECTL_IT_REGION overrides if you want to target a specific one.

The credentials need `iam:CreateRole` as well as the Glue actions, because a Glue job cannot be
created without a role Glue can assume and this module will not reuse a real one. A principal
holding only the Glue actions fails in the fixture rather than in a test.
"""

import contextlib
import json
import os
import time
import uuid

import boto3
import botocore.exceptions
import pytest

from dectl.commands.glue import DEFINITION_FIELDS
from dectl.commands.glue import NESTED_DEFINITION_FIELDS
from dectl.commands.glue import apply_glue_job_update
from dectl.commands.glue import plan_glue_job_update
from dectl.config import GlueJobConfig

pytestmark = pytest.mark.integration

GLUE_MANAGED_POLICY = 'arn:aws:iam::aws:policy/service-role/AWSGlueServiceRole'
GLUE_TRUST_POLICY = {
    'Version': '2012-10-17',
    'Statement': [
        {'Effect': 'Allow', 'Principal': {'Service': 'glue.amazonaws.com'}, 'Action': 'sts:AssumeRole'},
    ],
}


@pytest.fixture(scope='module')
def session():
    profile = os.environ.get('DECTL_IT_AWS_PROFILE')
    region = os.environ.get('DECTL_IT_REGION') or os.environ.get('AWS_REGION') or os.environ.get('AWS_DEFAULT_REGION')
    kwargs = {}
    if profile:
        kwargs['profile_name'] = profile
    if region:
        kwargs['region_name'] = region
    built = boto3.Session(**kwargs)
    if built.get_credentials() is None:
        pytest.skip('no AWS credentials available')
    # A profile that names a role and a source but no region resolves credentials and then
    # fails inside botocore's endpoint resolver, once per test, on a NoRegionError that names
    # neither the profile nor the variable that would fix it.
    if built.region_name is None:
        pytest.skip(f'profile {profile or "default"} resolves no region; set DECTL_IT_REGION')
    return built


@pytest.fixture(scope='module')
def glue_role_arn(session):
    iam = session.client('iam')
    role_name = f'dectl-it-glue-{uuid.uuid4().hex[:8]}'
    created = iam.create_role(RoleName=role_name, AssumeRolePolicyDocument=json.dumps(GLUE_TRUST_POLICY))
    iam.attach_role_policy(RoleName=role_name, PolicyArn=GLUE_MANAGED_POLICY)
    # A freshly created role is not immediately assumable by Glue (IAM is eventually
    # consistent); create_job below also retries, but a short wait avoids most churn.
    time.sleep(10)
    try:
        yield created['Role']['Arn']
    finally:
        iam.detach_role_policy(RoleName=role_name, PolicyArn=GLUE_MANAGED_POLICY)
        iam.delete_role(RoleName=role_name)


def create_job_with_role_retry(glue, **create_kwargs):
    # Glue rejects create_job with InvalidInputException until the new role has propagated
    # far enough to be assumed. Retry that specific transient case for up to ~90s.
    deadline = time.time() + 90
    while True:
        try:
            return glue.create_job(**create_kwargs)
        except botocore.exceptions.ClientError as exc:
            code = exc.response['Error']['Code']
            message = exc.response['Error']['Message'].lower()
            propagation_error = code == 'InvalidInputException' and 'assume' in message
            if propagation_error and time.time() < deadline:
                time.sleep(5)
                continue
            raise


# Each spec is a job type plus the capacity fields unique to it, and the subset of those
# fields we assert survive the deploy. glueetl is the important case: get_job returns a
# derived AllocatedCapacity alongside WorkerType, and if dectl failed to strip it the
# update_job call would raise "cannot set both" -- so a passing glueetl case proves the strip.
JOB_TYPE_SPECS = [
    pytest.param(
        {'Command': {'Name': 'pythonshell', 'PythonVersion': '3.9', 'ScriptLocation': 's3://placeholder/orig.py'}, 'MaxCapacity': 1.0},
        {'MaxCapacity': 1.0},
        id='pythonshell',
    ),
    pytest.param(
        {
            'Command': {'Name': 'glueetl', 'PythonVersion': '3', 'ScriptLocation': 's3://placeholder/orig.py'},
            'GlueVersion': '4.0',
            'WorkerType': 'G.1X',
            'NumberOfWorkers': 2,
        },
        {'GlueVersion': '4.0', 'WorkerType': 'G.1X', 'NumberOfWorkers': 2},
        id='glueetl',
    ),
]


@pytest.mark.parametrize('capacity_fields, expected_preserved', JOB_TYPE_SPECS)
def test_deploy_preserves_existing_job_definition(session, glue_role_arn, capacity_fields, expected_preserved):
    glue = session.client('glue')
    job_name = f'dectl-it-{uuid.uuid4().hex[:8]}'

    create_kwargs = {
        'Name': job_name,
        'Role': glue_role_arn,
        'Timeout': 60,
        'MaxRetries': 1,
        'ExecutionProperty': {'MaxConcurrentRuns': 3},
        'DefaultArguments': {'--TempDir': 's3://placeholder/tmp/', '--extra-existing': 'keep-me'},
        **capacity_fields,
    }
    create_job_with_role_retry(glue, **create_kwargs)
    try:
        glue_job = GlueJobConfig(
            name=job_name,
            script_bucket='dectl-it-scripts',
            script_prefix='scripts',
            scripts=['deployed.py'],
            role=glue_role_arn,
            arguments={'new-flag': 'on'},
        )
        job_update = plan_glue_job_update(session, glue_job, assume_yes=True)
        if job_update is not None:
            apply_glue_job_update(session, glue_job, job_update)

        job = glue.get_job(JobName=job_name)['Job']

        # Fields dectl does not manage must survive an UpdateJob (which replaces the definition).
        assert job['Timeout'] == 60
        assert job['MaxRetries'] == 1
        assert job['ExecutionProperty']['MaxConcurrentRuns'] == 3
        for key, value in expected_preserved.items():
            assert job[key] == value

        # Existing default arguments survive; dectl's --JOB_NAME and configured args are merged in.
        assert job['DefaultArguments']['--TempDir'] == 's3://placeholder/tmp/'
        assert job['DefaultArguments']['--extra-existing'] == 'keep-me'
        assert job['DefaultArguments']['--JOB_NAME'] == job_name
        assert job['DefaultArguments']['--new-flag'] == 'on'

        # The one thing deploy is supposed to change.
        assert job['Command']['ScriptLocation'] == 's3://dectl-it-scripts/scripts/deployed.py'
    finally:
        glue.delete_job(JobName=job_name)


@contextlib.contextmanager
def live_job(session, **create_kwargs):
    """A throwaway Glue job, deleted whatever the test does.

    Named by uuid rather than by test, so a run that dies between create and delete cannot
    collide with the next one and leave a second run failing on a name that already exists."""
    glue = session.client('glue')
    job_name = f'dectl-it-{uuid.uuid4().hex[:8]}'
    create_job_with_role_retry(glue, Name=job_name, **create_kwargs)
    try:
        yield job_name
    finally:
        glue.delete_job(JobName=job_name)


def deploy_definition(session, glue_job):
    """The definition half of `deploy`, and whether it had anything to do.

    False means `plan_glue_job_update` found nothing to change, which is the state the whole
    diff exists to reach: a deploy that is a pure script push with no drift surface."""
    job_update = plan_glue_job_update(session, glue_job, assume_yes=True)
    if job_update is None:
        return False
    apply_glue_job_update(session, glue_job, job_update)
    return True


def job_config(job_name, role, **managed):
    return GlueJobConfig(
        name=job_name,
        script_bucket='dectl-it-scripts',
        script_prefix='scripts',
        scripts=['deployed.py'],
        role=role,
        **managed,
    )


SPARK_JOB = {
    'Command': {'Name': 'glueetl', 'PythonVersion': '3', 'ScriptLocation': 's3://placeholder/orig.py'},
    'GlueVersion': '4.0',
    'WorkerType': 'G.1X',
    'NumberOfWorkers': 2,
}


# A config that leaves sizing unmanaged, and one that names the size the job already has. Both
# are ordinary and only the second is what a config generated from the job's own Terraform looks
# like — which is the one that stays broken if the suppression is decided by what the config
# names rather than by what the live job is.
SIZING_CONFIGS = [
    pytest.param({}, id='sizing_unmanaged'),
    pytest.param({'worker_type': 'G.1X', 'number_of_workers': 2}, id='sizing_named'),
]


@pytest.mark.parametrize('sizing', SIZING_CONFIGS)
def test_a_worker_sized_job_converges_after_one_deploy(session, glue_role_arn, sizing):
    """The steady state, against the derived MaxCapacity only real Glue produces.

    GetJob returns a MaxCapacity for a worker-sized job that UpdateJob refuses to take back, so
    dropping it is forced and reporting it as a removal is a row nothing can ever clear. No fake
    establishes this: a fake returns the definition it was given, and the derivation is the
    service's. A second deploy finding work to do is the defect."""
    with live_job(session, Role=glue_role_arn, **SPARK_JOB) as job_name:
        glue_job = job_config(job_name, glue_role_arn, **sizing)

        assert deploy_definition(session, glue_job) is True
        assert deploy_definition(session, glue_job) is False

        live = session.client('glue').get_job(JobName=job_name)['Job']
        assert live['WorkerType'] == 'G.1X'
        assert live['NumberOfWorkers'] == 2
        # Glue derives it back whatever dectl sends, which is why suppressing the row is the
        # only way the definition ever reads as converged.
        assert 'MaxCapacity' in live


# Each managed field, the definition it lands in, and a value the fixture above does not already
# hold. Driven against real Glue because a fake accepts whatever key it is handed: the question
# here is whether UpdateJob takes the field at all, and only the service answers that.
DEFINITION_FIELD_SPECS = [
    pytest.param({'glue_version': '5.0'}, lambda job: job['GlueVersion'] == '5.0', id='glue_version'),
    pytest.param({'timeout_minutes': 17}, lambda job: job['Timeout'] == 17, id='timeout_minutes'),
    pytest.param({'max_retries': 0}, lambda job: job['MaxRetries'] == 0, id='max_retries'),
    pytest.param({'execution_class': 'FLEX'}, lambda job: job['ExecutionClass'] == 'FLEX', id='execution_class'),
    pytest.param(
        {'max_concurrent_runs': 4},
        lambda job: job['ExecutionProperty']['MaxConcurrentRuns'] == 4,
        id='max_concurrent_runs',
    ),
    pytest.param(
        {'worker_type': 'G.2X', 'number_of_workers': 3},
        lambda job: job['WorkerType'] == 'G.2X' and job['NumberOfWorkers'] == 3,
        id='worker_pair',
    ),
]


@pytest.mark.parametrize('managed, holds', DEFINITION_FIELD_SPECS)
def test_a_managed_field_round_trips_through_update_job(session, glue_role_arn, managed, holds):
    with live_job(session, Role=glue_role_arn, **SPARK_JOB) as job_name:
        assert deploy_definition(session, job_config(job_name, glue_role_arn, **managed)) is True

        assert holds(session.client('glue').get_job(JobName=job_name)['Job'])


def test_every_managed_definition_field_is_driven_against_live_glue():
    """A field dectl writes and no live test drives is one whose first real UpdateJob is a user's.

    `max_capacity` and `python_version` are absent from the table because neither can be set on
    the Spark fixture the table runs against. The Python shell case below drives both."""
    driven = {field for spec in DEFINITION_FIELD_SPECS for field in spec.values[0]}

    assert set(DEFINITION_FIELDS) | set(NESTED_DEFINITION_FIELDS) == driven | {'max_capacity', 'python_version'}


def test_a_python_shell_job_takes_max_capacity_and_a_python_version(session, glue_role_arn):
    shell_job = {
        'Command': {'Name': 'pythonshell', 'PythonVersion': '3.9', 'ScriptLocation': 's3://placeholder/orig.py'},
        'MaxCapacity': 0.0625,
    }
    with live_job(session, Role=glue_role_arn, **shell_job) as job_name:
        glue_job = job_config(job_name, glue_role_arn, max_capacity=1.0, python_version='3.9')

        assert deploy_definition(session, glue_job) is True

        live = session.client('glue').get_job(JobName=job_name)['Job']
        assert live['MaxCapacity'] == 1.0
        assert live['Command']['PythonVersion'] == '3.9'


def test_migrating_a_dpu_sized_job_to_worker_sizing_is_accepted(session, glue_role_arn):
    """Both sizing models on one UpdateJob is what Glue refuses, so the DPU value has to go.

    Reading the sizing model off the live job rather than off the merged update sends both and
    is refused. That rejection is the service's rule and no fake holds it, which is what puts
    this here rather than beside the unit test for the same displacement.

    The fixture omits `GlueVersion` deliberately. Asking for one alongside `MaxCapacity` gets a
    job carrying `WorkerType` as well, which is already worker-sized and migrates nothing."""
    dpu_spark_job = {
        'Command': {'Name': 'glueetl', 'PythonVersion': '3', 'ScriptLocation': 's3://placeholder/orig.py'},
        'MaxCapacity': 4.0,
    }
    with live_job(session, Role=glue_role_arn, **dpu_spark_job) as job_name:
        glue = session.client('glue')
        assert 'WorkerType' not in glue.get_job(JobName=job_name)['Job']
        glue_job = job_config(job_name, glue_role_arn, worker_type='G.1X', number_of_workers=2)

        assert deploy_definition(session, glue_job) is True

        live = glue.get_job(JobName=job_name)['Job']
        assert live['WorkerType'] == 'G.1X'
        assert live['NumberOfWorkers'] == 2


def test_a_script_location_that_is_the_only_change_still_reaches_the_job(session, glue_role_arn):
    """The definition is read as converged if the diff is measured against a mutated before-image.

    UpdateJob is then skipped and the job keeps the location it had, with the new script sitting
    uploaded at the one nothing points to. Everything else here matches the live job exactly, so
    ScriptLocation is the only thing that can produce a change."""
    with live_job(session, Role=glue_role_arn, **SPARK_JOB) as job_name:
        glue_job = job_config(job_name, glue_role_arn)
        deploy_definition(session, glue_job)
        settled = session.client('glue').get_job(JobName=job_name)['Job']['Command']['ScriptLocation']

        moved = job_config(job_name, glue_role_arn)
        moved = moved.model_copy(update={'scripts': ['moved.py']})
        assert deploy_definition(session, moved) is True

        live = session.client('glue').get_job(JobName=job_name)['Job']
        assert settled == 's3://dectl-it-scripts/scripts/deployed.py'
        assert live['Command']['ScriptLocation'] == 's3://dectl-it-scripts/scripts/moved.py'
