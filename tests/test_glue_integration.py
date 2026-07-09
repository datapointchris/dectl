"""Live AWS round-trip test for the Glue deploy path.

Opt-in only: run with `uv run pytest --run-integration`. It creates a throwaway IAM
role and Glue job in the caller's AWS account, runs dectl's deploy against it, asserts
the job definition round-trips, then deletes everything. Glue create/update/get/delete
are free -- the test never starts a job run, so it costs nothing.

Region/profile come from the standard AWS environment (AWS_PROFILE, AWS_REGION), with
DECTL_IT_AWS_PROFILE / DECTL_IT_REGION overrides if you want to target a specific one.
"""

import json
import os
import time
import uuid

import boto3
import botocore.exceptions
import pytest

from dectl.commands.glue import update_glue_job
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
        update_glue_job(session, glue_job)

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
