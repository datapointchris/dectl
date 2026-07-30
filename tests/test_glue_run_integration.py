"""Live AWS test for the Glue log tailing path.

Opt-in only: `uv run pytest --run-integration`. It creates a throwaway bucket, IAM role and
Python Shell job in the caller's account, runs the job for real, tails its logs through dectl,
then deletes everything.

This one costs money, unlike the deploy test: a Python Shell run at 1/16 DPU for well under a
minute, so a fraction of a cent. It exists because the two bugs that shipped in this path were
both invisible to a mocked test:

* The tailer waited for a log *stream* to be created, sequentially per group, so a job that
  never wrote to stderr sat silent for the full 120-second timeout. A fake returns streams
  instantly, so the wait cost nothing in tests.
* Replacing that with filter_log_events and no startTime produced *correct* output after
  several minutes of paging, because both Glue groups are shared by every job in the account
  and the scan defaults to the start of retention. A fake holds three events, so the scan was
  instant and the test passed.

Both are failures of *latency against real data volume*, which is why the assertions here are
wall-clock bounded rather than only about content. Region/profile come from the standard AWS
environment, with DECTL_IT_AWS_PROFILE / DECTL_IT_REGION overrides.
"""

import json
import os
import time
import uuid

import boto3
import botocore.exceptions
import pytest

from dectl.commands.glue import GlueRunWatcher
from dectl.commands.glue import run_log_start
from dectl.logs import GLUE_OUTPUT_LOG_GROUP
from dectl.logs import tail_glue_run
from dectl.output import console

pytestmark = pytest.mark.integration

GLUE_MANAGED_POLICY = 'arn:aws:iam::aws:policy/service-role/AWSGlueServiceRole'
GLUE_TRUST_POLICY = {
    'Version': '2012-10-17',
    'Statement': [{'Effect': 'Allow', 'Principal': {'Service': 'glue.amazonaws.com'}, 'Action': 'sts:AssumeRole'}],
}

# A run that has finished must have its whole output readable within this. The failure mode being
# guarded is a scan of the shared group from the start of retention, which took minutes.
TAIL_BUDGET_SECONDS = 30
RUN_TIMEOUT_SECONDS = 600


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
def marker():
    """Unique token the job prints, so assertions cannot pass on another job's logs."""
    return f'dectl-it-{uuid.uuid4().hex[:8]}'


@pytest.fixture(scope='module')
def script_location(session, marker):
    # AWSGlueServiceRole grants S3 access to buckets named aws-glue-*, so naming the bucket that
    # way avoids attaching an inline policy just to let Glue read one object.
    bucket = f'aws-glue-{marker}'
    key = 'job.py'
    script = (
        'import sys\n'
        f'print("{marker}-stdout-first", flush=True)\n'
        f'sys.stderr.write("{marker}-stderr\\n")\n'
        'sys.stderr.flush()\n'
        f'print("{marker}-stdout-last", flush=True)\n'
    )

    s3 = session.client('s3')
    create_kwargs = {'Bucket': bucket}
    if session.region_name != 'us-east-1':
        create_kwargs['CreateBucketConfiguration'] = {'LocationConstraint': session.region_name}
    s3.create_bucket(**create_kwargs)
    s3.put_object(Bucket=bucket, Key=key, Body=script.encode())
    try:
        yield f's3://{bucket}/{key}'
    finally:
        s3.delete_object(Bucket=bucket, Key=key)
        s3.delete_bucket(Bucket=bucket)


@pytest.fixture(scope='module')
def glue_role_arn(session, marker):
    iam = session.client('iam')
    role_name = f'{marker}-role'
    created = iam.create_role(RoleName=role_name, AssumeRolePolicyDocument=json.dumps(GLUE_TRUST_POLICY))
    iam.attach_role_policy(RoleName=role_name, PolicyArn=GLUE_MANAGED_POLICY)
    time.sleep(10)  # IAM is eventually consistent; create_job below also retries.
    try:
        yield created['Role']['Arn']
    finally:
        iam.detach_role_policy(RoleName=role_name, PolicyArn=GLUE_MANAGED_POLICY)
        iam.delete_role(RoleName=role_name)


@pytest.fixture(scope='module')
def job_name(session, marker, glue_role_arn, script_location):
    glue = session.client('glue')
    name = f'{marker}-job'
    create_kwargs = {
        'Name': name,
        'Role': glue_role_arn,
        'Command': {'Name': 'pythonshell', 'PythonVersion': '3.9', 'ScriptLocation': script_location},
        'MaxCapacity': 0.0625,
        'Timeout': 10,
        # Glue holds a run's concurrency slot for a moment after it reports SUCCEEDED, so the
        # default of 1 makes a second run in the same module fail with ConcurrentRunsExceeded.
        'ExecutionProperty': {'MaxConcurrentRuns': 3},
    }

    deadline = time.time() + 120
    while True:
        try:
            glue.create_job(**create_kwargs)
            break
        except botocore.exceptions.ClientError as exc:
            message = exc.response['Error']['Message'].lower()
            if exc.response['Error']['Code'] == 'InvalidInputException' and 'assume' in message and time.time() < deadline:
                time.sleep(5)
                continue
            raise
    try:
        yield name
    finally:
        glue.delete_job(JobName=name)


def wait_for_run(glue, job_name: str, run_id: str) -> dict:
    deadline = time.time() + RUN_TIMEOUT_SECONDS
    while time.time() < deadline:
        run = glue.get_job_run(JobName=job_name, RunId=run_id)['JobRun']
        if run['JobRunState'] in ('SUCCEEDED', 'FAILED', 'STOPPED', 'TIMEOUT', 'ERROR'):
            return run
        time.sleep(10)
    raise AssertionError(f'run {run_id} did not finish within {RUN_TIMEOUT_SECONDS}s')


@pytest.fixture(scope='module')
def finished_run(session, job_name):
    """One completed run, shared by the tests below so the suite pays for a single Glue run."""
    glue = session.client('glue')
    run_id = glue.start_job_run(JobName=job_name)['JobRunId']
    run = wait_for_run(glue, job_name, run_id)
    assert run['JobRunState'] == 'SUCCEEDED', f'job failed: {run.get("ErrorMessage")}'
    # CloudWatch ingestion lags the process that wrote the lines.
    time.sleep(15)
    return run_id


def test_run_log_streams_are_named_for_the_run(session, finished_run):
    # The whole prefix-filtering approach rests on this: a Python Shell run's stream in each
    # shared group is named after the run id. If Glue ever changes that, every other assertion
    # here fails mysteriously, so state it directly.
    logs = session.client('logs')
    streams = logs.describe_log_streams(logGroupName=GLUE_OUTPUT_LOG_GROUP, logStreamNamePrefix=finished_run)

    assert [s['logStreamName'] for s in streams['logStreams']] == [finished_run]


def test_tail_prints_stdout_and_stderr_of_a_finished_run(session, job_name, finished_run, marker):
    logs = session.client('logs')
    started_at = run_log_start(session.client('glue'), job_name, finished_run)

    began = time.time()
    with console.capture() as capture:
        tail_glue_run(logs, finished_run, started_at, follow=False)
    elapsed = time.time() - began
    output = capture.get()

    assert f'{marker}-stdout-first' in output
    assert f'{marker}-stdout-last' in output
    # stderr lands in the other group, which only exists once something writes to it — the case
    # the original sequential stream-wait stalled two minutes on.
    assert f'{marker}-stderr' in output
    assert 'err ' in output, 'error-group lines should carry the err prefix'
    assert elapsed < TAIL_BUDGET_SECONDS, f'tailing a finished run took {elapsed:.0f}s'


def test_tail_preserves_program_order_within_a_group(session, job_name, finished_run, marker):
    """Lines from one group render in the order the job printed them.

    Note what this deliberately does *not* claim. The job writes stdout, stderr, then stdout
    again, but the tail renders both stdout lines before the stderr one: Glue ships the two
    groups through independent agents, so a CloudWatch timestamp records when a line was
    *shipped*, not when it was written. Program order across the two groups is not recoverable
    from the data, and dectl does not pretend otherwise — the err prefix is what makes the
    interleaving readable instead. Within a single group the timestamps are monotonic, and that
    is what is worth pinning here; the merge itself is covered by unit tests, where equal
    timestamps have a defined tiebreak that real CloudWatch data does not.
    """
    logs = session.client('logs')
    started_at = run_log_start(session.client('glue'), job_name, finished_run)

    with console.capture() as capture:
        tail_glue_run(logs, finished_run, started_at, follow=False)
    output = capture.get()

    assert output.index(f'{marker}-stdout-first') < output.index(f'{marker}-stdout-last')
    # Glue's own setup lines share the output group and precede the script's.
    assert output.index('Setup complete') < output.index(f'{marker}-stdout-first')


def test_following_a_live_run_streams_and_stops_on_its_own(session, job_name, marker):
    """The full `run --follow` loop against a run started for this test.

    Bounded by the run's own timeout: if the tail failed to notice the terminal state it would
    hang here rather than fail fast, so the budget is what makes the assertion meaningful."""
    glue = session.client('glue')
    run_id = glue.start_job_run(JobName=job_name)['JobRunId']
    watcher = GlueRunWatcher(glue, job_name, run_id)
    started_at = run_log_start(glue, job_name, run_id)

    began = time.time()
    with console.capture() as capture:
        tail_glue_run(session.client('logs'), run_id, started_at, follow=True, run_finished=watcher.finished)
    elapsed = time.time() - began
    output = capture.get()

    assert watcher.state == 'SUCCEEDED'
    assert f'{marker}-stdout-first' in output
    assert f'{marker}-stdout-last' in output
    assert elapsed < RUN_TIMEOUT_SECONDS, f'follow did not return promptly after the run ended ({elapsed:.0f}s)'
