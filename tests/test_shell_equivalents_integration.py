"""Live AWS round trip for the blocks `dectl PIPELINE commands` prints that write.

Opt-in: `uv run pytest --run-integration`. It creates a bucket, a Glue job, a standard and a
durable Lambda function and three state machines, prints the blocks for a config naming them, runs
each in bash, and deletes everything. The offline test pins that a block sends what its verb
sends, and refuses and falls back where its verb does. This one runs the blocks against the
services, whose answers decide what a block does next: whether a definition is unchanged, which
revision to publish, which version an alias names, and which earlier version holds the newest
execution.

It costs a Python Shell run stopped as it starts, a few Lambda invocations and one Step Functions
execution, which fails at its first state because the flow invokes a function in no real account.

Needs aws (v2), bash, jq and python3. The config is the committed layered one's sales pipeline,
with each name replaced by a resource the test created. Each role is a variable or a created role,
as `conftest.py` describes; the Lambda one is `DECTL_IT_LAMBDA_ROLE_ARN`.
"""

import contextlib
import io
import json
import os
import shutil
import subprocess
import time
import uuid
import zipfile
from pathlib import Path
from typing import NamedTuple

import pytest
from botocore.exceptions import ClientError

from dectl.commands.glue import resolve_scripts
from dectl.commands.glue import upload_scripts
from dectl.config import load_config
from dectl.env import render_env_model
from dectl.env import set_active_environment
from tests.conftest import LAYERED_CONFIG
from tests.test_glue_integration import create_job_with_role_retry
from tests.test_shell_equivalents import CONFIG_DIR
from tests.test_shell_equivalents import ENVIRONMENT
from tests.test_shell_equivalents import printed_blocks
from tests.test_shell_equivalents import resolved_live_state
from tests.test_stepfunctions_integration import PASS_DEFINITION
from tests.test_stepfunctions_integration import create_machine_with_role_retry
from tests.test_stepfunctions_integration import settled_routing

MISSING_TOOLS = [tool for tool in ('aws', 'bash', 'jq', 'python3') if shutil.which(tool) is None]

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(bool(MISSING_TOOLS), reason=f'the printed blocks need {", ".join(MISSING_TOOLS)}'),
]

PIPELINE = 'it'
SCRIPT_KEY = 'scripts/load.py'

# A machine for each path through the printed deploy: one differing, one deployed plain, and one
# created with the rendered definition, which Step Functions then holds with no revisionId.
MACHINES = ('changed', 'plain', 'fresh')

# Code a deploy replaces, answering differently from the committed handlers.
BEFORE = {'stage': 'before'}
PLAIN_HANDLER = f'def handler(event, context):\n    return {BEFORE!r}\n'
DURABLE_HANDLER = (
    'from aws_durable_execution_sdk_python import durable_execution\n\n\n'
    f'@durable_execution\ndef handler(event, context):\n    return {BEFORE!r}\n'
)


class Live(NamedTuple):
    blocks: dict[str, str]
    environment: dict[str, str]
    config_path: Path
    prefix: str


def live_config(account: str, region: str, prefix: str) -> dict:
    """The sales pipeline's resources, named for this run, with the committed flow's substitutions."""
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr('dectl.config.CONFIG_PATH', LAYERED_CONFIG)
        committed = load_config()
    assert committed is not None
    flow = committed.pipelines['sales'].step_functions['flow']
    defaults = {'account_id': account, 'region': region}
    if os.environ.get('DECTL_IT_AWS_PROFILE'):
        defaults['aws_profile'] = os.environ['DECTL_IT_AWS_PROFILE']
    machine = {
        'definition_file': flow.definition_file,
        'definition_substitutions': flow.definition_substitutions,
        'live_alias': 'live',
    }
    return {
        'defaults': defaults,
        'pipelines': {
            PIPELINE: {
                'resolve_paths_from': str(CONFIG_DIR),
                'glue_jobs': {
                    'load': {
                        'name': f'{prefix}-load',
                        'script_bucket': prefix,
                        'script_prefix': 'scripts',
                        'scripts': ['load.py'],
                        'role': f'arn:aws:iam::{account}:role/{prefix}-glue',
                    }
                },
                'lambdas': {
                    'process': {'name': f'{prefix}-process', 'source_dir': 'lambdas/process', 'live_alias': 'live'},
                    'workflow': {'name': f'{prefix}-workflow', 'source_dir': 'lambdas/workflow', 'live_alias': 'live', 'durable': True},
                },
                'step_functions': {scenario: {'name': f'{prefix}-{scenario}', **machine} for scenario in MACHINES},
            }
        },
    }


@pytest.fixture(scope='module')
def live(session, tmp_path_factory) -> Live:
    """The printed blocks, and the caller's own AWS environment to run them in."""
    scratch = tmp_path_factory.mktemp('live')
    configured_prefix = f'dectl-it-{{env}}-{uuid.uuid4().hex[:8]}'
    account = session.client('sts').get_caller_identity()['Account']
    config_path = scratch / 'config' / 'dectl' / 'config.yaml'
    config_path.parent.mkdir(parents=True)
    config_path.write_text(json.dumps(live_config(account, session.region_name, configured_prefix), indent=2))
    environment = os.environ | {
        'XDG_CONFIG_HOME': str(scratch / 'config'),
        'XDG_CACHE_HOME': str(scratch / 'cache'),
        'AWS_PAGER': '',
        # What a block prints without an --output is display, so the test fixes the format it parses.
        'AWS_DEFAULT_OUTPUT': 'json',
    }
    blocks = printed_blocks(environment, PIPELINE)
    return Live(blocks, environment, config_path, configured_prefix.replace('{env}', ENVIRONMENT))


def run_block(live: Live, *words: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ['bash', '-c', live.blocks[' '.join(['dectl', PIPELINE, *words])]],
        capture_output=True,
        text=True,
        env=live.environment,
        timeout=600,
    )


def run_cleanly(live: Live, *words: str) -> str:
    ran = run_block(live, *words)
    assert ran.returncode == 0, f'{" ".join(words)} exited {ran.returncode}:\n{ran.stderr}'
    return ran.stdout


@pytest.fixture
def script_bucket(session, live):
    s3 = session.client('s3')
    create_kwargs: dict = {'Bucket': live.prefix}
    if session.region_name != 'us-east-1':
        create_kwargs['CreateBucketConfiguration'] = {'LocationConstraint': session.region_name}
    s3.create_bucket(**create_kwargs)
    try:
        yield live.prefix
    finally:
        for page in s3.get_paginator('list_objects_v2').paginate(Bucket=live.prefix):
            for item in page.get('Contents', []):
                s3.delete_object(Bucket=live.prefix, Key=item['Key'])
        s3.delete_bucket(Bucket=live.prefix)


@pytest.fixture
def glue_job(session, live, glue_role_arn, script_bucket):
    glue = session.client('glue')
    name = f'{live.prefix}-load'
    create_job_with_role_retry(
        glue,
        Name=name,
        Role=glue_role_arn,
        Command={'Name': 'pythonshell', 'PythonVersion': '3.9', 'ScriptLocation': f's3://{script_bucket}/{SCRIPT_KEY}'},
        MaxCapacity=0.0625,
        Timeout=5,
    )
    try:
        yield name
    finally:
        glue.delete_job(JobName=name)


def test_the_printed_glue_blocks_upload_what_dectl_uploads_and_start_a_run(session, live, script_bucket, glue_job, monkeypatch):
    s3 = session.client('s3')
    run_cleanly(live, 'glue', 'load', 'deploy')
    assert s3.get_object(Bucket=script_bucket, Key=SCRIPT_KEY)['Body'].read() == (CONFIG_DIR / 'load.py').read_bytes()
    printed = s3.head_object(Bucket=script_bucket, Key=SCRIPT_KEY)

    monkeypatch.setattr('dectl.config.CONFIG_PATH', live.config_path)
    config = load_config()
    assert config is not None
    set_active_environment(ENVIRONMENT, '--env')
    pipeline = config.pipelines[PIPELINE]
    upload_scripts(session, render_env_model(pipeline.glue_jobs['load']), resolve_scripts(PIPELINE, pipeline, 'load'))
    uploaded = s3.head_object(Bucket=script_bucket, Key=SCRIPT_KEY)
    assert (printed['ContentType'], printed['ETag']) == (uploaded['ContentType'], uploaded['ETag'])

    glue = session.client('glue')
    run_id = json.loads(run_cleanly(live, 'glue', 'load', 'run'))['JobRunId']
    try:
        assert glue.get_job_run(JobName=glue_job, RunId=run_id)['JobRun']['Id'] == run_id
    finally:
        glue.batch_stop_job_run(JobName=glue_job, JobRunIds=[run_id])


def archive(module: str, source: str) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w') as built:
        built.writestr(f'{module}.py', source)
    return buffer.getvalue()


@contextlib.contextmanager
def live_function(session, role_arn: str, name: str, module: str, source: str, **create_kwargs):
    """A function answering BEFORE, published as version 1 with a `live` alias, deleted afterwards."""
    client = session.client('lambda')
    deadline = time.time() + 90
    while True:
        try:
            created = client.create_function(
                FunctionName=name,
                Runtime='python3.13',
                Role=role_arn,
                Handler=f'{module}.handler',
                Code={'ZipFile': archive(module, source)},
                Timeout=30,
                Publish=True,
                **create_kwargs,
            )
            break
        except ClientError as exc:
            # A new role cannot be assumed by Lambda until IAM has propagated it.
            if 'assume' in exc.response['Error']['Message'].lower() and time.time() < deadline:
                time.sleep(5)
                continue
            raise
    logs = session.client('logs')
    try:
        client.get_waiter('function_active_v2').wait(FunctionName=name)
        client.create_alias(FunctionName=name, Name='live', FunctionVersion=created['Version'])
        yield client
    finally:
        client.delete_function(FunctionName=name)
        with contextlib.suppress(logs.exceptions.ResourceNotFoundException):
            logs.delete_log_group(logGroupName=f'/aws/lambda/{name}')


def invoked(client, name: str, qualifier: str = '$LATEST') -> dict:
    return json.loads(client.invoke(FunctionName=name, Qualifier=qualifier)['Payload'].read())


def printed_payload(printed: str) -> object:
    """The function's response, which a run block prints last."""
    return json.loads(printed.splitlines()[-1])


def test_the_printed_lambda_blocks_deploy_invoke_and_publish(session, live, lambda_role_arn):
    name = f'{live.prefix}-process'
    with live_function(session, lambda_role_arn, name, 'process_handler', PLAIN_HANDLER) as client:
        run_cleanly(live, 'lambda', 'process', 'deploy')
        client.get_waiter('function_updated_v2').wait(FunctionName=name)
        assert invoked(client, name) == {'orders': []}
        assert invoked(client, name, 'live') == BEFORE

        assert printed_payload(run_cleanly(live, 'lambda', 'process', 'run')) == {'orders': []}

        run_cleanly(live, 'lambda', 'process', 'deploy', '--publish')
        assert client.get_alias(FunctionName=name, Name='live')['FunctionVersion'] == '2'
        assert invoked(client, name, 'live') == {'orders': []}


def test_the_printed_durable_blocks_publish_run_and_find_the_execution(session, live, lambda_role_arn):
    name = f'{live.prefix}-workflow'
    durable = {'DurableConfig': {'ExecutionTimeout': 60, 'RetentionPeriodInDays': 1}}
    with live_function(session, lambda_role_arn, name, 'workflow_handler', DURABLE_HANDLER, **durable) as client:
        run_cleanly(live, 'lambda', 'workflow', 'deploy', '--publish')
        assert client.get_alias(FunctionName=name, Name='live')['FunctionVersion'] == '2'

        started = run_cleanly(live, 'lambda', 'workflow', 'run')
        assert printed_payload(started) == {'stage': 'done'}

        [execution] = json.loads(run_cleanly(live, 'lambda', 'workflow', 'executions'))['DurableExecutions']
        assert execution['FunctionArn'].endswith(f':{name}:2')
        assert started.splitlines()[0] == f'durable execution: {execution["DurableExecutionArn"]}'
        assert json.loads(run_cleanly(live, 'lambda', 'workflow', 'history'))['Events']

        # Right after deploy --publish moves the alias, its version holds no execution, so both
        # blocks find the run on version 2 through the sweep.
        client.update_function_configuration(FunctionName=name, Environment={'Variables': {'STAGE': 'after'}})
        client.get_waiter('function_updated_v2').wait(FunctionName=name)
        run_cleanly(live, 'lambda', 'workflow', 'deploy', '--publish')
        assert client.get_alias(FunctionName=name, Name='live')['FunctionVersion'] == '3'
        assert json.loads(run_cleanly(live, 'lambda', 'workflow', 'history'))['Events']
        # The role writes no logs, so the group is one nothing has created.
        assert 'does not exist' in run_cleanly(live, 'lambda', 'workflow', 'logs')


@pytest.fixture
def machines(session, live, states_role_arn):
    sfn = session.client('stepfunctions')
    rendered = resolved_live_state()
    arns: dict[str, str] = {}
    try:
        for scenario in MACHINES:
            created = create_machine_with_role_retry(
                sfn,
                name=f'{live.prefix}-{scenario}',
                definition=rendered if scenario == 'fresh' else PASS_DEFINITION,
                roleArn=states_role_arn,
                type='STANDARD',
                publish=True,
            )
            arns[scenario] = created['stateMachineArn']
            sfn.create_state_machine_alias(
                name='live', routingConfiguration=[{'stateMachineVersionArn': created['stateMachineVersionArn'], 'weight': 100}]
            )
        yield arns
    finally:
        for arn in arns.values():
            sfn.delete_state_machine(stateMachineArn=arn)


def test_the_printed_state_machine_blocks_plan_deploy_publish_and_start(session, live, machines):
    sfn = session.client('stepfunctions')
    rendered = resolved_live_state()

    def described(scenario: str) -> dict:
        return sfn.describe_state_machine(stateMachineArn=machines[scenario])

    def versions(scenario: str) -> list[str]:
        listed = sfn.list_state_machine_versions(stateMachineArn=machines[scenario])['stateMachineVersions']
        return sorted(version['stateMachineVersionArn'] for version in listed)

    assert '+  "QueryLanguage": "JSONata",' in run_cleanly(live, 'sfn', 'plain', 'plan')
    run_cleanly(live, 'sfn', 'plain', 'deploy')
    assert described('plain')['definition'] == rendered
    assert run_cleanly(live, 'sfn', 'plain', 'plan') == ''

    changed = machines['changed']
    run_cleanly(live, 'sfn', 'changed', 'deploy', '--publish')
    [route] = settled_routing(sfn, f'{changed}:live', away_from=f'{changed}:1')
    assert route['stateMachineVersionArn'] == f'{changed}:2'
    assert sfn.describe_state_machine(stateMachineArn=f'{changed}:2')['definition'] == rendered
    revision = described('changed')['revisionId']
    run_cleanly(live, 'sfn', 'changed', 'deploy')
    run_cleanly(live, 'sfn', 'changed', 'deploy', '--publish')
    assert described('changed')['revisionId'] == revision
    assert versions('changed') == [f'{changed}:1', f'{changed}:2']

    fresh = machines['fresh']
    run_cleanly(live, 'sfn', 'fresh', 'deploy', '--publish')
    assert 'revisionId' not in described('fresh')
    assert versions('fresh') == [f'{fresh}:1']

    started = json.loads(run_cleanly(live, 'sfn', 'changed', 'run'))['executionArn']
    assert [execution['executionArn'] for execution in sfn.list_executions(stateMachineArn=changed)['executions']] == [started]
