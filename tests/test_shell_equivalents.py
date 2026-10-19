"""Each block `dectl PIPELINE commands` prints makes the calls its verb makes.

The verb runs through dectl, and the block through bash and the real aws CLI, jq, python3 and
curl. Both send to one recording endpoint that answers them alike, under different credentials,
and the test compares what each asked for. A write must match exactly, because a block writing
something else is the bug this verb could ship. A read the block makes must be one the verb makes.
The verb may read more, because dectl also reads to report and to refuse.

Runs on the committed layered config's sales pipeline under `--env prod`. It configures every
resource type, so every kind of block is driven."""

import gzip
import hashlib
import io
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any
from typing import NamedTuple

import pytest

from dectl.asl import render_definition
from dectl.commands.glue import build_job_update
from dectl.commands.stepfunctions import read_definition_template
from dectl.config import load_config
from dectl.env import DEFAULT_ENV
from dectl.env import render_env_model
from dectl.env import set_active_environment
from tests.conftest import LAYERED_CONFIG
from tests.recording_endpoint import Call
from tests.recording_endpoint import RecordingEndpoint
from tests.recording_endpoint import Reply
from tests.recording_endpoint import error_reply
from tests.test_iceberg import four_commit_table

PIPELINE = 'sales'
ENVIRONMENT = 'prod'
CONFIG_DIR = LAYERED_CONFIG.parent
DECTL = Path(sys.executable).parent / 'dectl'
TOOLS = ('aws', 'bash', 'curl', 'jq', 'python3')
# The shells the header promises. zsh is not on CI's runner, so its half runs where it is installed.
SHELLS = ('bash', 'zsh')

pytestmark = pytest.mark.skipif(
    any(shutil.which(tool) is None for tool in TOOLS) and not os.environ.get('CI'),
    reason=f'runs the printed blocks, so it needs {", ".join(TOOLS)}; CI has them and runs this',
)

REGION = 'us-east-2'
ACCOUNT = '123456789012'
JENKINS_TOKEN = 'jenkins-api-token'
RUN_ID = 'jr_5f0c1d'
# Half a second past the minute, so a block that loses the milliseconds converting between the
# CLI's printed time and the API's epoch starts its logs at a different instant from the verb.
STARTED = 1760860980.5
FIRST_EVENT_MS = 1760860981250
LIVE_VERSION = '3'
PUBLISHED_VERSION = '4'
METADATA_BUCKET = 'acme-prod-sales-lake'
METADATA_KEY = 'events/metadata/00004-c0ffee.gz.metadata.json'
METADATA = gzip.compress(json.dumps(four_commit_table()).encode())
OBJECT_HEADERS = {'ETag': '"c0ffee"', 'Last-Modified': 'Sun, 19 Oct 2025 07:43:00 GMT'}

# The parameters that say how much to read or how it travelled, never what is read or written.
TRANSFER_PARAMS = frozenset(
    {
        'ChecksumAlgorithm',
        'ChecksumCRC32',
        'ChecksumCRC32C',
        'ChecksumCRC64NVME',
        'ChecksumMode',
        'ChecksumSHA1',
        'ChecksumSHA256',
        'ContentEncoding',
        'ContentLength',
        'ContentMD5',
        'IfMatch',
        'Range',
    }
)
PAGING_PARAMS = frozenset({'Limit', 'Marker', 'MaxItems', 'MaxResults', 'NextToken', 'interleaved', 'limit', 'maxResults', 'nextToken'})
# `aws s3 cp` sizes an object before it downloads it.
TRANSFER_READS = frozenset({('s3', 'HeadObject')})
READ_PREFIXES = ('Get', 'List', 'Describe', 'Head', 'Filter', 'Validate')


class MachineScenario(NamedTuple):
    """What the live state machine holds when a definition block runs."""

    name: str
    live_is_rendered: bool
    revision_id: str | None


DIFFERS = MachineScenario('differs', live_is_rendered=False, revision_id='revision-1')
UNCHANGED = MachineScenario('unchanged', live_is_rendered=True, revision_id='revision-1')
# Created by Terraform from this file and never updated since, so it carries no revisionId.
NEVER_UPDATED = MachineScenario('never-updated', live_is_rendered=True, revision_id=None)


class Case(NamedTuple):
    command: str
    scenario: MachineScenario
    named_by_scenario: bool

    @property
    def id(self) -> str:
        verb = self.command.removeprefix(f'dectl {PIPELINE} ')
        return f'{verb} [{self.scenario.name}]' if self.named_by_scenario else verb


def isolated_environment(config_home: Path, scratch: Path, caller: str = 'COLLECTOR', endpoint_url: str = '') -> dict[str, str]:
    """A process environment reaching only the endpoint, under `caller`'s credentials."""
    environment = {key: value for key, value in os.environ.items() if not key.startswith(('AWS_', 'DECTL_', 'JENKINS_'))}
    return environment | {
        'PATH': f'{scratch.parent / "bin"}{os.pathsep}{environment["PATH"]}',
        'HOME': str(scratch / 'home'),
        'XDG_CONFIG_HOME': str(config_home),
        'XDG_CACHE_HOME': str(scratch / 'cache'),
        'AWS_ENDPOINT_URL': endpoint_url or 'http://127.0.0.1:9',
        'AWS_ACCESS_KEY_ID': caller,
        'AWS_SECRET_ACCESS_KEY': 'not-a-secret',
        'AWS_CONFIG_FILE': os.devnull,
        'AWS_SHARED_CREDENTIALS_FILE': os.devnull,
        'AWS_EC2_METADATA_DISABLED': 'true',
        # StartLiveTail is sent to a `streaming-` host, which a local endpoint has no name for.
        'AWS_DISABLE_HOST_PREFIX_INJECTION': 'true',
        'AWS_PAGER': '',
        'JENKINS_USER': caller,
        'JENKINS_API_TOKEN': JENKINS_TOKEN,
        'FAKE_COMMAND_LOG': str(scratch / f'{caller}.commands'),
    }


def write_config(config_home: Path, jenkins_url: str) -> None:
    (config_home / 'dectl').mkdir(parents=True, exist_ok=True)
    text = LAYERED_CONFIG.read_text().replace('url: https://jenkins.example.com', f'url: {jenkins_url}')
    (config_home / 'dectl' / 'config.yaml').write_text(text)


def printed_blocks(environment: dict[str, str], pipeline: str = PIPELINE) -> dict[str, str]:
    """Each command `commands --json` names, and the shell it prints for it."""
    printed = subprocess.run(
        [str(DECTL), '--env', ENVIRONMENT, pipeline, 'commands', '--json'],
        capture_output=True,
        text=True,
        env=environment,
        cwd=CONFIG_DIR,
        check=True,
    )
    return {block['command']: block['shell'] for block in json.loads(printed.stdout)['commands']}


def collect_cases() -> list[Case]:
    with tempfile.TemporaryDirectory() as scratch:
        root = Path(scratch)
        write_config(root / 'config', 'https://jenkins.example.com')
        commands = printed_blocks(isolated_environment(root / 'config', root / 'collector'))
    cases = []
    for command in commands:
        words = command.split()
        if words[2:3] == ['sfn'] and words[4:5] == ['deploy']:
            scenarios = [DIFFERS, UNCHANGED, NEVER_UPDATED] if '--publish' in words else [DIFFERS, UNCHANGED]
        else:
            scenarios = [DIFFERS]
        cases += [Case(command, scenario, len(scenarios) > 1) for scenario in scenarios]
    return cases


CASES = collect_cases()
MONITOR = next(case for case in CASES if case.command == f'dectl {PIPELINE} monitor')


class LiveState(NamedTuple):
    """What the endpoint holds for the sales pipeline under prod: each Glue job converged with the
    config, so no deploy sends UpdateJob, and the flow's rendered definition."""

    glue_jobs: dict[str, dict]
    rendered_definition: str


def resolved_live_state() -> LiveState:
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr('dectl.config.CONFIG_PATH', LAYERED_CONFIG)
        config = load_config()
    assert config is not None
    set_active_environment(ENVIRONMENT, '--env')
    try:
        pipeline = config.pipelines[PIPELINE]
        jobs = {}
        for configured in pipeline.glue_jobs.values():
            job = render_env_model(configured)
            jobs[job.name] = {'Name': job.name, **build_job_update({'Command': {'Name': 'pythonshell'}}, job).definition}
        flow = render_env_model(pipeline.step_functions['flow'])
        assert flow.definition_file is not None
        template = read_definition_template(CONFIG_DIR / flow.definition_file)
        return LiveState(jobs, render_definition(template, flow.definition_substitutions).text)
    finally:
        set_active_environment(DEFAULT_ENV, 'default')


def function_configuration(name: str, version: str = '$LATEST') -> dict:
    return {
        'FunctionName': name,
        'FunctionArn': f'arn:aws:lambda:{REGION}:{ACCOUNT}:function:{name}',
        'Version': version,
        'State': 'Active',
        'LastUpdateStatus': 'Successful',
        'Timeout': 30,
    }


def durable_execution(function_name: str) -> dict:
    arn = f'arn:aws:lambda:{REGION}:{ACCOUNT}:function:{function_name}:{LIVE_VERSION}/durable-execution/nightly/7a1e'
    return {
        'DurableExecutionArn': arn,
        'DurableExecutionName': 'nightly',
        'FunctionArn': f'arn:aws:lambda:{REGION}:{ACCOUNT}:function:{function_name}:{LIVE_VERSION}',
        'Status': 'SUCCEEDED',
        'StartTimestamp': STARTED,
        'EndTimestamp': STARTED + 90,
    }


def live_machine(arn: str, scenario: MachineScenario, state: LiveState) -> dict:
    definition = (
        state.rendered_definition if scenario.live_is_rendered else state.rendered_definition.replace('Loads a day', 'Loaded a day')
    )
    machine = {
        'stateMachineArn': arn,
        'name': arn.rpartition(':')[2],
        'status': 'ACTIVE',
        'definition': definition,
        'roleArn': f'arn:aws:iam::{ACCOUNT}:role/states',
        'type': 'STANDARD',
        'creationDate': STARTED,
    }
    return machine | ({'revisionId': scenario.revision_id} if scenario.revision_id else {})


def aws_reply(call: Call, scenario: MachineScenario, state: LiveState) -> Reply | None:
    params = call.params
    job_run = {'Id': RUN_ID, 'JobName': params.get('JobName'), 'StartedOn': STARTED, 'JobRunState': 'SUCCEEDED'}
    execution_arn = f'{params.get("stateMachineArn", "").replace(":stateMachine:", ":execution:")}:run-1'
    match call.service, call.operation:
        case 'glue', 'GetJob':
            return Reply(body={'Job': state.glue_jobs[params['JobName']]})
        case 'glue', 'GetJobRuns':
            return Reply(body={'JobRuns': [job_run]})
        case 'glue', 'GetJobRun':
            return Reply(body={'JobRun': job_run})
        case 'glue', 'StartJobRun':
            return Reply(body={'JobRunId': RUN_ID})
        case 'glue', 'GetTable':
            parameters = {'table_type': 'ICEBERG', 'metadata_location': f's3://{METADATA_BUCKET}/{METADATA_KEY}'}
            return Reply(body={'Table': {'Name': params['Name'], 'DatabaseName': params['DatabaseName'], 'Parameters': parameters}})
        case 's3', 'PutObject':
            return Reply(headers={'ETag': '"c0ffee"'})
        case 's3', 'HeadObject':
            return Reply(headers={**OBJECT_HEADERS, 'Content-Length': str(len(METADATA))})
        case 's3', 'GetObject':
            return Reply(body=METADATA, headers=OBJECT_HEADERS)
        case 'logs', 'DescribeLogStreams':
            stream = {'logStreamName': '2025/10/19/[$LATEST]c0ffee', 'firstEventTimestamp': FIRST_EVENT_MS}
            return Reply(body={'logStreams': [stream | {'lastEventTimestamp': FIRST_EVENT_MS + 2000}]})
        case 'logs', 'FilterLogEvents':
            return Reply(body={'events': [], 'searchedLogStreams': []})
        case 'logs', 'StartLiveTail':
            return error_reply('AccessDeniedException', 'this endpoint holds a live tail request but serves no stream')
        case 'lambda', 'GetFunction':
            return Reply(body={'Configuration': function_configuration(params['FunctionName'])})
        case 'lambda', 'GetFunctionConfiguration' | 'UpdateFunctionCode':
            return Reply(body=function_configuration(params['FunctionName']))
        case 'lambda', 'PublishVersion':
            return Reply(201, function_configuration(params['FunctionName'], PUBLISHED_VERSION))
        case 'lambda', 'GetAlias' | 'UpdateAlias':
            version = params.get('FunctionVersion', LIVE_VERSION)
            alias_arn = f'arn:aws:lambda:{REGION}:{ACCOUNT}:function:{params["FunctionName"]}:{params["Name"]}'
            return Reply(body={'AliasArn': alias_arn, 'Name': params['Name'], 'FunctionVersion': version})
        case 'lambda', 'Invoke':
            headers = {'X-Amz-Executed-Version': LIVE_VERSION if 'Qualifier' in params else '$LATEST'}
            if 'Qualifier' in params:
                headers['X-Amz-Durable-Execution-Arn'] = durable_execution(params['FunctionName'])['DurableExecutionArn']
            return Reply(body={'processed': 0}, headers=headers)
        case 'lambda', 'ListDurableExecutionsByFunction':
            return Reply(body={'DurableExecutions': [durable_execution(params['FunctionName'])]})
        case 'lambda', 'GetDurableExecution':
            function_name = params['DurableExecutionArn'].split(':function:')[1].split(':')[0]
            return Reply(body=durable_execution(function_name))
        case 'lambda', 'GetDurableExecutionHistory':
            return Reply(body={'Events': []})
        case 'stepfunctions', 'DescribeStateMachine':
            return Reply(body=live_machine(params['stateMachineArn'], scenario, state))
        case 'stepfunctions', 'ValidateStateMachineDefinition':
            return Reply(body={'result': 'OK', 'diagnostics': []})
        case 'stepfunctions', 'UpdateStateMachine':
            return Reply(body={'updateDate': STARTED, 'revisionId': 'revision-2'})
        case 'stepfunctions', 'PublishStateMachineVersion':
            return Reply(body={'creationDate': STARTED, 'stateMachineVersionArn': f'{params["stateMachineArn"]}:{PUBLISHED_VERSION}'})
        case 'stepfunctions', 'DescribeStateMachineAlias':
            machine_arn = params['stateMachineAliasArn'].rpartition(':')[0]
            routing = [{'stateMachineVersionArn': f'{machine_arn}:{LIVE_VERSION}', 'weight': 100}]
            alias = {'stateMachineAliasArn': params['stateMachineAliasArn'], 'name': 'live', 'routingConfiguration': routing}
            return Reply(body=alias | {'creationDate': STARTED, 'updateDate': STARTED})
        case 'stepfunctions', 'UpdateStateMachineAlias':
            return Reply(body={'updateDate': STARTED})
        case 'stepfunctions', 'StartExecution':
            return Reply(body={'executionArn': execution_arn, 'startDate': STARTED})
        case 'stepfunctions', 'ListExecutions':
            execution = {'executionArn': execution_arn, 'stateMachineArn': params['stateMachineArn'], 'name': 'run-1'}
            return Reply(body={'executions': [execution | {'status': 'SUCCEEDED', 'startDate': STARTED}]})
        case 'stepfunctions', 'GetExecutionHistory':
            return Reply(body={'events': []})
    return None


def jenkins_reply(call: Call) -> Reply | None:
    if call.operation.startswith('POST ') and call.operation.endswith('/buildWithParameters'):
        return Reply(201, '', {'Location': 'http://127.0.0.1/queue/item/7/'})
    if call.operation.endswith('/lastBuild/api/json'):
        return Reply(body={'number': 7, 'displayName': '#7', 'building': False, 'result': 'SUCCESS'})
    if call.operation.endswith('/lastBuild/logText/progressiveText'):
        console = 'Started by user deployer\nFinished: SUCCESS\n'
        return Reply(body=console, headers={'X-Text-Size': str(len(console)), 'X-More-Data': 'false'})
    return None


class Run(NamedTuple):
    returncode: int
    stdout: str
    stderr: str
    calls: list[Call]
    commands: list[list[str]]


class Observation(NamedTuple):
    """One verb's run, and its printed block's run under each installed shell."""

    text: str
    verb: Run
    blocks: dict[str, Run]


def finished_run(process: subprocess.CompletedProcess[str] | subprocess.Popen[str], endpoint, caller, scratch, out='', err='') -> Run:
    log = scratch / f'{caller}.commands'
    commands = [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []
    stdout = out if isinstance(process, subprocess.Popen) else process.stdout
    stderr = err if isinstance(process, subprocess.Popen) else process.stderr
    return Run(process.returncode, stdout, stderr, endpoint.calls_from(caller), commands)


def run_monitor_until_every_group_is_polled(argv, environment, endpoint, caller) -> tuple[subprocess.Popen[str], str, str]:
    """`monitor` follows until interrupted, so it is stopped once one group has been polled twice,
    which is after every group was polled once."""
    process = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=environment, cwd=CONFIG_DIR)
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        polled = Counter(call.params.get('logGroupName') for call in endpoint.calls_from(caller) if call.operation == 'FilterLogEvents')
        if polled and max(polled.values()) > 1:
            break
        time.sleep(0.2)
    process.terminate()
    out, err = process.communicate(timeout=10)
    return process, out, err


def callers(index: int) -> list[str]:
    return [f'VERB{index}', *(f'{shell.upper()}{index}' for shell in SHELLS)]


def observe(index: int, case: Case, text: str, endpoint: RecordingEndpoint, config_home: Path, root: Path) -> Observation:
    """The verb, then its block under each shell, all sharing one HOME so a mountpoint is one path."""
    scratch = root / f'case-{index}'
    (scratch / 'home').mkdir(parents=True)
    verb_caller, *shell_callers = callers(index)
    argv = [str(DECTL), '--env', ENVIRONMENT, *shlex.split(case.command)[1:]]

    environment = isolated_environment(config_home, scratch, verb_caller, endpoint.url)
    if case is MONITOR:
        process, out, err = run_monitor_until_every_group_is_polled(argv, environment, endpoint, verb_caller)
        verb = finished_run(process, endpoint, verb_caller, scratch, out, err)
    else:
        completed = subprocess.run(
            argv, capture_output=True, text=True, env=environment, cwd=CONFIG_DIR, stdin=subprocess.DEVNULL, timeout=120
        )
        verb = finished_run(completed, endpoint, verb_caller, scratch)

    blocks = {}
    for shell, caller in zip(SHELLS, shell_callers, strict=True):
        if shutil.which(shell) is None:
            continue
        environment = isolated_environment(config_home, scratch, caller, endpoint.url)
        completed = subprocess.run([shell, '-c', text], capture_output=True, text=True, env=environment, cwd=CONFIG_DIR, timeout=120)
        blocks[shell] = finished_run(completed, endpoint, caller, scratch)
    return Observation(text, verb, blocks)


FAKE_COMMAND = """\
#!/usr/bin/env python3
import json, os, sys
with open(os.environ['FAKE_COMMAND_LOG'], 'a') as log:
    log.write(json.dumps([os.path.basename(sys.argv[0]), *sys.argv[1:]]) + '\\n')
"""


@pytest.fixture(scope='module')
def observed(tmp_path_factory) -> dict[str, Observation]:
    """Every case run at once, each verb then its block, since each is a few processes of waiting."""
    root = tmp_path_factory.mktemp('shell-equivalents')
    (root / 'bin').mkdir()
    for name in ('mount-s3', 'umount'):
        fake = root / 'bin' / name
        fake.write_text(FAKE_COMMAND)
        fake.chmod(0o755)
    state = resolved_live_state()
    scenarios = {caller: case.scenario for index, case in enumerate(CASES) for caller in callers(index)}

    def respond(call: Call) -> Reply | None:
        if call.service == 'jenkins':
            return jenkins_reply(call)
        return aws_reply(call, scenarios[call.caller], state)

    with RecordingEndpoint(respond) as endpoint:
        config_home = root / 'config'
        write_config(config_home, endpoint.url)
        blocks = printed_blocks(isolated_environment(config_home, root / 'printer', 'PRINTER', endpoint.url))
        with ThreadPoolExecutor(max_workers=min(16, (os.cpu_count() or 4) * 2)) as pool:
            futures = {
                case.id: pool.submit(observe, index, case, blocks[case.command], endpoint, config_home, root)
                for index, case in enumerate(CASES)
            }
            return {case_id: future.result() for case_id, future in futures.items()}


def writes(call: Call) -> bool:
    if call.service == 'jenkins':
        return call.operation.startswith('POST ')
    return not call.operation.startswith(READ_PREFIXES) and call.operation != 'StartLiveTail'


def canonical(call: Call, dropped: frozenset[str] = TRANSFER_PARAMS) -> str:
    """A call as text two callers' versions of it compare equal on."""
    params: dict[str, Any] = {key: value for key, value in call.params.items() if key not in dropped}
    if 'logGroupIdentifier' in params:
        params['logGroupName'] = params.pop('logGroupIdentifier')
    if call.operation == 'UpdateFunctionCode':
        with zipfile.ZipFile(io.BytesIO(params['ZipFile'])) as archive:
            params['ZipFile'] = {info.filename: hashlib.sha256(archive.read(info)).hexdigest() for info in archive.infolist()}
    if call.operation == 'Invoke':
        params.setdefault('InvocationType', 'RequestResponse')
        params['Payload'] = json.loads(params.get('Payload') or b'null')
    if call.service == 'jenkins':
        params['credentials'] = '$JENKINS_USER:$JENKINS_API_TOKEN' if params['credentials'] == f'{call.caller}:{JENKINS_TOKEN}' else '?'
    return json.dumps([call.service, call.operation, params], sort_keys=True, default=lambda raw: hashlib.sha256(raw).hexdigest())


def write_set(run: Run) -> Counter[str]:
    return Counter(canonical(call) for call in run.calls if writes(call))


def read_set(run: Run) -> set[str]:
    return {
        canonical(call, TRANSFER_PARAMS | PAGING_PARAMS | {'endTime'})
        for call in run.calls
        if not writes(call) and (call.service, call.operation) not in TRANSFER_READS
    }


def expected_block_exit(case: Case) -> int:
    # `diff` reports the difference the plan exists to show, and exits 1 for it.
    return 1 if case.command.endswith(' sfn flow plan') and not case.scenario.live_is_rendered else 0


def block_run(seen: Observation, shell: str) -> Run:
    if shell not in seen.blocks:
        pytest.skip(f'{shell} is not installed')
    return seen.blocks[shell]


@pytest.mark.parametrize('shell', SHELLS)
@pytest.mark.parametrize('case', [case for case in CASES if case is not MONITOR], ids=lambda case: case.id)
def test_each_printed_block_makes_the_calls_its_verb_makes(case, shell, observed):
    seen = observed[case.id]
    block = block_run(seen, shell)
    assert seen.verb.returncode == 0, seen.verb.stderr
    assert block.returncode == expected_block_exit(case), block.stderr
    assert write_set(block) == write_set(seen.verb)
    assert read_set(block) <= read_set(seen.verb), seen.text
    assert block.commands == seen.verb.commands
    if case.command.endswith(' s3 export'):
        assert seen.text == seen.verb.stdout
    elif case.command.endswith(' uri'):
        assert block.stdout == seen.verb.stdout


@pytest.mark.parametrize('shell', SHELLS)
def test_the_monitor_block_tails_every_group_monitor_polls(shell, observed):
    seen = observed[MONITOR.id]
    polled = {call.params['logGroupName'] for call in seen.verb.calls if call.operation == 'FilterLogEvents'}
    tailed = [call.params['logGroupIdentifiers'] for call in block_run(seen, shell).calls if call.operation == 'StartLiveTail']
    assert len(tailed) == 1
    prefix = f'arn:aws:logs:{REGION}:{ACCOUNT}:log-group:'
    assert all(arn.startswith(prefix) for arn in tailed[0])
    assert {arn.removeprefix(prefix) for arn in tailed[0]} == polled


TREE_SCRIPT = """\
import json, sys
import typer
from dectl.main import app

def leaves(group, path):
    for name, command in group.commands.items():
        if hasattr(command, 'commands'):
            yield from leaves(command, [*path, name])
        else:
            yield ' '.join([*path, name])

pipeline = sys.argv[1]
print(json.dumps(sorted(leaves(typer.main.get_command(app).commands[pipeline], ['dectl', pipeline]))))
"""


def test_every_verb_of_the_pipeline_has_a_block(tmp_path):
    """Read off the command tree, so a verb added to dectl without a block fails here."""
    write_config(tmp_path / 'config', 'https://jenkins.example.com')
    environment = isolated_environment(tmp_path / 'config', tmp_path / 'scratch')
    tree = subprocess.run([sys.executable, '-c', TREE_SCRIPT, PIPELINE], capture_output=True, text=True, env=environment, check=True)
    verbs = set(json.loads(tree.stdout)) - {f'dectl {PIPELINE} list', f'dectl {PIPELINE} commands'}
    printed = {case.command.removesuffix(' --publish') for case in CASES}
    assert printed == verbs


def test_the_printed_invoke_sends_one_request_where_the_cli_alone_retries(tmp_path):
    """A retried invoke runs the function again. The CLI retries a failed call by default, which the
    control arm shows, so it is the block's own AWS_MAX_ATTEMPTS=1 that holds it to one."""
    write_config(tmp_path / 'config', 'https://jenkins.example.com')
    (tmp_path / 'scratch' / 'home').mkdir(parents=True)

    def respond(call: Call) -> Reply:
        return error_reply('ServiceException', 'the function failed after it started', status=500)

    with RecordingEndpoint(respond) as endpoint:
        environment = isolated_environment(tmp_path / 'config', tmp_path / 'scratch', 'ONCE', endpoint.url)
        shell = printed_blocks(environment)[f'dectl {PIPELINE} lambda process run']
        assert 'AWS_MAX_ATTEMPTS=1 ' in shell
        for caller, text in [('ONCE', shell), ('RETRIED', shell.replace('AWS_MAX_ATTEMPTS=1 ', ''))]:
            environment = isolated_environment(tmp_path / 'config', tmp_path / 'scratch', caller, endpoint.url)
            subprocess.run(['bash', '-c', text], capture_output=True, text=True, env=environment, cwd=CONFIG_DIR, timeout=120)
        once = [call for call in endpoint.calls_from('ONCE') if call.operation == 'Invoke']
        retried = [call for call in endpoint.calls_from('RETRIED') if call.operation == 'Invoke']
    assert len(once) == 1
    assert len(retried) > 1


def test_the_printed_zip_leaves_out_what_the_deploy_leaves_out(tmp_path):
    """The committed sources hold nothing a deploy skips, so this source holds each kind it skips."""
    source = tmp_path / 'source'
    for relative in ('handler.py', 'pkg/model.py', '__pycache__/handler.cpython-313.pyc', 'pkg/__pycache__/model.pyc', '.git/config'):
        (source / relative).parent.mkdir(parents=True, exist_ok=True)
        (source / relative).write_text(relative)
    config_home = tmp_path / 'config'
    (config_home / 'dectl').mkdir(parents=True)
    (config_home / 'dectl' / 'config.yaml').write_text(
        f'defaults:\n  account_id: "{ACCOUNT}"\n  region: {REGION}\n'
        f'pipelines:\n  tiny:\n    lambdas:\n      handler:\n        name: tiny-handler\n        source_dir: {source}\n'
    )
    scratch = tmp_path / 'scratch'
    (scratch / 'home').mkdir(parents=True)

    with RecordingEndpoint(lambda call: Reply(body=function_configuration(call.params['FunctionName']))) as endpoint:
        environment = isolated_environment(config_home, scratch, 'VERB', endpoint.url)
        deployed = subprocess.run(
            [str(DECTL), '--env', ENVIRONMENT, 'tiny', 'lambda', 'handler', 'deploy'], capture_output=True, text=True, env=environment
        )
        assert deployed.returncode == 0, deployed.stderr
        text = printed_blocks(environment, 'tiny')['dectl tiny lambda handler deploy']
        environment = isolated_environment(config_home, scratch, 'BLOCK', endpoint.url)
        ran = subprocess.run(['bash', '-c', text], capture_output=True, text=True, env=environment)
        assert ran.returncode == 0, ran.stderr
        uploads = {caller: [canonical(call) for call in endpoint.calls_from(caller) if writes(call)] for caller in ('VERB', 'BLOCK')}
    assert len(uploads['VERB']) == 1
    assert uploads['BLOCK'] == uploads['VERB']
