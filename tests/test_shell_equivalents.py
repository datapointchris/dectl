"""Each block `dectl PIPELINE commands` prints makes the calls its verb makes.

The verb runs through dectl, and the block through bash and the real aws CLI, jq, python3 and
curl. Both send to one recording endpoint that answers them alike, under different credentials,
and the test compares what each asked for. A write must match exactly, because a block writing
something else is the bug this verb could ship. A read the block makes must be one the verb makes.
The verb may read more, because dectl also reads to report and to refuse.

Each verb runs in the state where every check passes, and in each state where it refuses or falls
back to another lookup. In a refusal both exit non-zero and write the same, which is nothing
before the refusing call.

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
from dectl.commands.shell_equivalents import ICEBERG_FILTERS
from dectl.commands.stepfunctions import read_definition_template
from dectl.config import load_config
from dectl.env import DEFAULT_ENV
from dectl.env import render_env_model
from dectl.env import set_active_environment
from dectl.iceberg import epoch_datetime
from dectl.iceberg import retention_summary
from tests.conftest import LAYERED_CONFIG
from tests.recording_endpoint import Call
from tests.recording_endpoint import RecordingEndpoint
from tests.recording_endpoint import Reply
from tests.recording_endpoint import error_reply
from tests.test_iceberg import rolled_back_table

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
PREVIOUS_VERSION = '2'
PUBLISHED_VERSION = '4'
FUNCTION_VERSIONS = ('$LATEST', '1', PREVIOUS_VERSION, LIVE_VERSION)
METADATA_BUCKET = 'acme-prod-sales-lake'
METADATA_KEY = 'events/metadata/00004-c0ffee.gz.metadata.json'


def table_metadata() -> bytes:
    """Rolled back, so the lineage and the snapshot log disagree, with its tag written ahead of its
    branches, so a block that does not sort the refs prints them in another order."""
    table = rolled_back_table()
    table['refs'] = dict(reversed(table['refs'].items()))
    return gzip.compress(json.dumps(table).encode())


METADATA = table_metadata()
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
READ_DROPPED = TRANSFER_PARAMS | PAGING_PARAMS | {'endTime'}
# `aws s3 cp` sizes an object before it downloads it.
TRANSFER_READS = frozenset({('s3', 'HeadObject')})
READ_PREFIXES = ('Get', 'List', 'Describe', 'Head', 'Filter', 'Validate')
# The reads whose answer a logs or history verb prints, so a block skipping one shows here.
ANSWERING_READS = frozenset({'FilterLogEvents', 'GetDurableExecutionHistory', 'GetExecutionHistory'})
# Writes a verb decides on from what it read, where its block stops at a comment instead.
DECIDED_BY_DECTL = {('glue', 'deploy'): frozenset({'UpdateJob'})}
# What a verb is run with here: a Glue deploy confirms a definition change, which nothing here
# answers, and an iceberg read's rows are held to its block's.
VERB_OPTIONS = {('glue', 'deploy'): ['--yes'], **{('iceberg', verb): ['--json'] for verb in ICEBERG_FILTERS}}


class Scenario(NamedTuple):
    """What the endpoint holds when a verb and its block run, and whether the verb refuses there."""

    name: str
    refuses: bool = False
    live_is_rendered: bool = False
    revision_id: str | None = 'revision-1'
    live_alias_exists: bool = True
    validation_allowed: bool = True
    definition_valid: bool = True
    job_exists: bool = True
    function_fails: bool = False
    ran: bool = True
    execution_version: str = LIVE_VERSION
    log_groups_written: bool = True


# Every lookup finds something, and the live definition differs from the rendered one.
CHECKS_PASS = Scenario('checks pass')
UNCHANGED = Scenario('definition unchanged', live_is_rendered=True)
# Created by Terraform from this file and never updated since, so it carries no revisionId.
NEVER_UPDATED = Scenario('never updated', live_is_rendered=True, revision_id=None)
NO_LIVE_ALIAS = Scenario('live alias missing', refuses=True, live_alias_exists=False)
VALIDATION_DENIED = Scenario('validation denied', validation_allowed=False)
DEFINITION_REJECTED = Scenario('definition rejected', refuses=True, definition_valid=False)
# Deployed before Terraform has created the job in this environment.
JOB_MISSING = Scenario('job missing', refuses=True, job_exists=False)
FUNCTION_FAILS = Scenario('function error', refuses=True, function_fails=True)
NOTHING_RAN = Scenario('nothing ran', refuses=True, ran=False)
# Right after deploy --publish moves the alias, the live version holds no execution yet.
ON_PREVIOUS_VERSION = Scenario('execution on a previous version', execution_version=PREVIOUS_VERSION)
UNWRITTEN_LOG_GROUPS = Scenario('log groups never written', log_groups_written=False)


class Case(NamedTuple):
    command: str
    scenario: Scenario
    named_by_scenario: bool

    @property
    def id(self) -> str:
        verb = self.command.removeprefix(f'dectl {PIPELINE} ')
        return f'{verb} [{self.scenario.name}]' if self.named_by_scenario else verb


def resource_and_verb(command: str) -> tuple[str, str]:
    words = [word for word in command.split()[2:] if word != '--publish']
    return words[0], words[-1]


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


def write_tiny_config(config_home: Path, resources: str) -> None:
    """A config holding one pipeline, `tiny`, whose resources are the YAML given, indented under it."""
    (config_home / 'dectl').mkdir(parents=True, exist_ok=True)
    body = ''.join(f'    {line}\n' for line in resources.splitlines())
    (config_home / 'dectl' / 'config.yaml').write_text(
        f'defaults:\n  account_id: "{ACCOUNT}"\n  region: {REGION}\npipelines:\n  tiny:\n{body}'
    )


def run_commands(environment: dict[str, str], pipeline: str = PIPELINE, cwd: Path = CONFIG_DIR) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(DECTL), '--env', ENVIRONMENT, pipeline, 'commands', '--json'], capture_output=True, text=True, env=environment, cwd=cwd
    )


def printed_blocks(environment: dict[str, str], pipeline: str = PIPELINE) -> dict[str, str]:
    """Each command `commands --json` names, and the shell it prints for it."""
    printed = run_commands(environment, pipeline)
    assert printed.returncode == 0, printed.stderr
    return {block['command']: block['shell'] for block in json.loads(printed.stdout)['commands']}


def scenarios_for(command: str, durable_aliases: set[str]) -> list[Scenario]:
    """The states a verb runs in: every check passing, and each state it refuses or falls back in."""
    match command.split()[2:]:
        case ['sfn', _, 'plan', '--publish']:
            return [CHECKS_PASS, NO_LIVE_ALIAS]
        case ['sfn', _, 'deploy']:
            return [CHECKS_PASS, UNCHANGED, VALIDATION_DENIED, DEFINITION_REJECTED]
        case ['glue', _, 'deploy']:
            return [CHECKS_PASS, JOB_MISSING]
        case ['sfn', _, 'deploy', '--publish']:
            return [CHECKS_PASS, UNCHANGED, NEVER_UPDATED, NO_LIVE_ALIAS]
        case ['sfn', _, 'logs']:
            return [CHECKS_PASS, NOTHING_RAN]
        case ['glue', _, 'logs']:
            return [CHECKS_PASS, NOTHING_RAN, UNWRITTEN_LOG_GROUPS]
        case ['lambda', _, 'run']:
            return [CHECKS_PASS, FUNCTION_FAILS]
        case ['lambda', _, 'history']:
            return [CHECKS_PASS, ON_PREVIOUS_VERSION, NOTHING_RAN]
        case ['lambda', alias, 'logs'] if alias in durable_aliases:
            return [CHECKS_PASS, ON_PREVIOUS_VERSION, NOTHING_RAN, UNWRITTEN_LOG_GROUPS]
    return [CHECKS_PASS]


def collect_cases() -> list[Case]:
    with tempfile.TemporaryDirectory() as scratch:
        root = Path(scratch)
        write_config(root / 'config', 'https://jenkins.example.com')
        commands = printed_blocks(isolated_environment(root / 'config', root / 'collector'))
    durable_aliases = {words[3] for words in map(str.split, commands) if words[2:5:2] == ['lambda', 'executions']}
    cases = []
    for command in commands:
        scenarios = scenarios_for(command, durable_aliases)
        cases += [Case(command, scenario, len(scenarios) > 1) for scenario in scenarios]
    return cases


CASES = collect_cases()
MONITOR = next(case for case in CASES if case.command == f'dectl {PIPELINE} monitor')


def resolved_live_state() -> str:
    """The flow's definition rendered for the sales pipeline under prod."""
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr('dectl.config.CONFIG_PATH', LAYERED_CONFIG)
        config = load_config()
    assert config is not None
    set_active_environment(ENVIRONMENT, '--env')
    try:
        flow = render_env_model(config.pipelines[PIPELINE].step_functions['flow'])
        assert flow.definition_file is not None
        template = read_definition_template(CONFIG_DIR / flow.definition_file)
        return render_definition(template, flow.definition_substitutions).text
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


def durable_execution(function_name: str, version: str) -> dict:
    arn = f'arn:aws:lambda:{REGION}:{ACCOUNT}:function:{function_name}:{version}/durable-execution/nightly/7a1e'
    return {
        'DurableExecutionArn': arn,
        'DurableExecutionName': 'nightly',
        'FunctionArn': f'arn:aws:lambda:{REGION}:{ACCOUNT}:function:{function_name}:{version}',
        'Status': 'SUCCEEDED',
        'StartTimestamp': STARTED,
        'EndTimestamp': STARTED + 90,
    }


def live_machine(arn: str, scenario: Scenario, rendered_definition: str) -> dict:
    definition = rendered_definition if scenario.live_is_rendered else rendered_definition.replace('Loads a day', 'Loaded a day')
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


def aws_reply(call: Call, scenario: Scenario, rendered_definition: str) -> Reply | None:
    params = call.params
    job_run = {'Id': RUN_ID, 'JobName': params.get('JobName'), 'StartedOn': STARTED, 'JobRunState': 'SUCCEEDED'}
    execution_arn = f'{params.get("stateMachineArn", "").replace(":stateMachine:", ":execution:")}:run-1'
    unwritten = error_reply('ResourceNotFoundException', 'The specified log group does not exist.')
    match call.service, call.operation:
        case 'glue', 'GetJob':
            if not scenario.job_exists:
                return error_reply('EntityNotFoundException', f'Job {params["JobName"]} not found')
            # A job the config would change, so a deploy sends UpdateJob.
            return Reply(body={'Job': {'Name': params['JobName'], 'Command': {'Name': 'pythonshell'}}})
        case 'glue', 'UpdateJob':
            return Reply(body={'JobName': params['JobName']})
        case 'glue', 'GetJobRuns':
            return Reply(body={'JobRuns': [job_run] if scenario.ran else []})
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
            if not scenario.log_groups_written:
                return unwritten
            stream = {'logStreamName': '2025/10/19/[$LATEST]c0ffee', 'firstEventTimestamp': FIRST_EVENT_MS}
            return Reply(body={'logStreams': [stream | {'lastEventTimestamp': FIRST_EVENT_MS + 2000}]})
        case 'logs', 'FilterLogEvents':
            return Reply(body={'events': [], 'searchedLogStreams': []}) if scenario.log_groups_written else unwritten
        case 'logs', 'StartLiveTail':
            return error_reply('AccessDeniedException', 'this endpoint holds a live tail request but serves no stream')
        case 'lambda', 'GetFunction':
            return Reply(body={'Configuration': function_configuration(params['FunctionName'])})
        case 'lambda', 'GetFunctionConfiguration' | 'UpdateFunctionCode':
            return Reply(body=function_configuration(params['FunctionName']))
        case 'lambda', 'ListVersionsByFunction':
            return Reply(body={'Versions': [function_configuration(params['FunctionName'], version) for version in FUNCTION_VERSIONS]})
        case 'lambda', 'PublishVersion':
            return Reply(201, function_configuration(params['FunctionName'], PUBLISHED_VERSION))
        case 'lambda', 'GetAlias' | 'UpdateAlias':
            version = params.get('FunctionVersion', LIVE_VERSION)
            alias_arn = f'arn:aws:lambda:{REGION}:{ACCOUNT}:function:{params["FunctionName"]}:{params["Name"]}'
            return Reply(body={'AliasArn': alias_arn, 'Name': params['Name'], 'FunctionVersion': version})
        case 'lambda', 'Invoke':
            headers = {'X-Amz-Executed-Version': LIVE_VERSION if 'Qualifier' in params else '$LATEST'}
            if 'Qualifier' in params:
                headers['X-Amz-Durable-Execution-Arn'] = durable_execution(params['FunctionName'], LIVE_VERSION)['DurableExecutionArn']
            if scenario.function_fails:
                headers['X-Amz-Function-Error'] = 'Unhandled'
                return Reply(body={'errorMessage': 'the handler raised', 'errorType': 'ValueError'}, headers=headers)
            return Reply(body={'processed': 0}, headers=headers)
        case 'lambda', 'ListDurableExecutionsByFunction':
            holds = scenario.ran and params['Qualifier'] == scenario.execution_version
            executions = [durable_execution(params['FunctionName'], scenario.execution_version)] if holds else []
            return Reply(body={'DurableExecutions': executions})
        case 'lambda', 'GetDurableExecution':
            function_name, version = params['DurableExecutionArn'].split(':function:')[1].split('/')[0].split(':')
            return Reply(body=durable_execution(function_name, version))
        case 'lambda', 'GetDurableExecutionHistory':
            return Reply(body={'Events': []})
        case 'stepfunctions', 'DescribeStateMachine':
            return Reply(body=live_machine(params['stateMachineArn'], scenario, rendered_definition))
        case 'stepfunctions', 'ValidateStateMachineDefinition':
            if not scenario.validation_allowed:
                return error_reply('AccessDeniedException', 'not authorized to perform: states:ValidateStateMachineDefinition')
            if not scenario.definition_valid:
                diagnostic = {
                    'severity': 'ERROR',
                    'code': 'SCHEMA_VALIDATION_FAILED',
                    'message': 'no state named Load',
                    'location': '/StartAt',
                }
                return Reply(body={'result': 'FAIL', 'diagnostics': [diagnostic]})
            return Reply(body={'result': 'OK', 'diagnostics': []})
        case 'stepfunctions', 'UpdateStateMachine':
            return Reply(body={'updateDate': STARTED, 'revisionId': 'revision-2'})
        case 'stepfunctions', 'PublishStateMachineVersion':
            return Reply(body={'creationDate': STARTED, 'stateMachineVersionArn': f'{params["stateMachineArn"]}:{PUBLISHED_VERSION}'})
        case 'stepfunctions', 'DescribeStateMachineAlias':
            if not scenario.live_alias_exists:
                return error_reply('ResourceNotFound', f'Resource not found: {params["stateMachineAliasArn"]}')
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
            executions = [execution | {'status': 'SUCCEEDED', 'startDate': STARTED}] if scenario.ran else []
            return Reply(body={'executions': executions})
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
    argv = [str(DECTL), '--env', ENVIRONMENT, *shlex.split(case.command)[1:], *VERB_OPTIONS.get(resource_and_verb(case.command), [])]

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
    rendered_definition = resolved_live_state()
    scenarios = {caller: case.scenario for index, case in enumerate(CASES) for caller in callers(index)}

    def respond(call: Call) -> Reply | None:
        if call.service == 'jenkins':
            return jenkins_reply(call)
        return aws_reply(call, scenarios[call.caller], rendered_definition)

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


def write_set(run: Run, decided: frozenset[str] = frozenset()) -> Counter[str]:
    return Counter(canonical(call) for call in run.calls if writes(call) and call.operation not in decided)


def read_set(run: Run) -> set[str]:
    return {
        canonical(call, READ_DROPPED) for call in run.calls if not writes(call) and (call.service, call.operation) not in TRANSFER_READS
    }


def answering_reads(run: Run) -> set[str]:
    return {canonical(call, READ_DROPPED) for call in run.calls if call.operation in ANSWERING_READS}


def block_run(seen: Observation, shell: str) -> Run:
    if shell not in seen.blocks:
        pytest.skip(f'{shell} is not installed')
    return seen.blocks[shell]


@pytest.mark.parametrize('shell', SHELLS)
@pytest.mark.parametrize('case', [case for case in CASES if case is not MONITOR], ids=lambda case: case.id)
def test_each_printed_block_makes_the_calls_its_verb_makes(case, shell, observed):
    seen = observed[case.id]
    block = block_run(seen, shell)
    assert (seen.verb.returncode != 0) == case.scenario.refuses, seen.verb.stderr
    assert (block.returncode != 0) == case.scenario.refuses, block.stderr
    assert write_set(block) == write_set(seen.verb, DECIDED_BY_DECTL.get(resource_and_verb(case.command), frozenset()))
    assert read_set(block) <= read_set(seen.verb), seen.text
    assert answering_reads(block) == answering_reads(seen.verb), seen.text
    assert block.commands == seen.verb.commands
    if case.command.endswith(' s3 export'):
        assert seen.text == seen.verb.stdout
    elif case.command.endswith(' uri'):
        assert block.stdout == seen.verb.stdout


def held_to(block: Any, verb: Any, where: str = 'rows') -> None:
    """Assert a block's jq output carries its verb's --json rows: every field the verb prints, with a
    timestamp as the epoch milliseconds it was printed from and a retention as the ref's own fields."""
    if isinstance(verb, list):
        assert isinstance(block, list) and len(block) == len(verb), where
        for index, (block_row, verb_row) in enumerate(zip(block, verb, strict=True)):
            held_to(block_row, verb_row, f'{where}[{index}]')
        return
    if not isinstance(verb, dict) or where.endswith('.summary'):
        assert block == verb, where
        return
    assert isinstance(block, dict), where
    if 'timestamp' in verb:
        millis = block['timestamp_ms']
        assert verb['timestamp'] == (None if millis is None else str(epoch_datetime(millis))), f'{where}.timestamp'
    if 'retention' in verb:
        ref = {'min-snapshots-to-keep': block['min_snapshots_to_keep'], 'max-snapshot-age-ms': block['max_snapshot_age_ms']}
        assert verb['retention'] == retention_summary(ref | {'max-ref-age-ms': block['max_ref_age_ms']}), f'{where}.retention'
    for key in verb.keys() - {'timestamp', 'retention'}:
        assert key in block, f'{where}.{key}'
        held_to(block[key], verb[key], f'{where}.{key}')


@pytest.mark.parametrize('case', [case for case in CASES if resource_and_verb(case.command)[0] == 'iceberg'], ids=lambda case: case.id)
def test_each_iceberg_block_prints_its_verbs_rows_for_a_rolled_back_table(case, observed):
    seen = observed[case.id]
    block = block_run(seen, 'bash')
    held_to(json.loads(block.stdout), json.loads(seen.verb.stdout))


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


TEMPLATE_MISSING_A_SUBSTITUTION = '{"StartAt": "${first_state}", "States": {}}\n'


@pytest.mark.parametrize('reach', ['a relative lambda source run from another directory', 'a template missing a substitution'])
def test_commands_refuses_a_pipeline_its_deploy_doors_refuse(reach, tmp_path):
    """A block naming a file that does not resolve from here, or a definition dectl cannot render,
    would run against the wrong file or send nothing deploy would send."""
    config_home = tmp_path / 'config'
    if reach == 'a relative lambda source run from another directory':
        write_tiny_config(config_home, 'lambdas:\n  handler:\n    name: tiny-handler\n    source_dir: lambdas/handler')
        pipeline, expected = 'tiny', 'lambdas/handler'
    else:
        template = tmp_path / 'flow.asl.json.tftpl'
        template.write_text(TEMPLATE_MISSING_A_SUBSTITUTION)
        write_tiny_config(config_home, f'step_functions:\n  flow:\n    name: tiny-flow\n    definition_file: {template}')
        pipeline, expected = 'tiny', '${first_state}'
    ran = run_commands(isolated_environment(config_home, tmp_path / 'scratch'), pipeline, cwd=tmp_path)
    assert ran.returncode == 1
    assert ran.stdout == ''
    assert expected in ran.stderr


def test_commands_warns_that_an_env_named_by_no_bucket_changed_nothing(tmp_path):
    """A bucket naming its environment outright is the same bucket under every --env."""
    write_tiny_config(tmp_path / 'config', 'buckets:\n  raw: acme-dev-raw')
    ran = run_commands(isolated_environment(tmp_path / 'config', tmp_path / 'scratch'), 'tiny', cwd=tmp_path)
    assert ran.returncode == 0, ran.stderr
    assert f'--env={ENVIRONMENT} changed nothing' in ran.stderr


def test_the_printed_invoke_sends_one_request_to_a_function_that_fails_after_it_starts(tmp_path):
    """A retried invoke runs the function again, and the aws CLI retries a 500 unless told not to."""
    write_config(tmp_path / 'config', 'https://jenkins.example.com')
    (tmp_path / 'scratch' / 'home').mkdir(parents=True)

    def respond(call: Call) -> Reply:
        return error_reply('ServiceException', 'the function failed after it started', status=500)

    with RecordingEndpoint(respond) as endpoint:
        environment = isolated_environment(tmp_path / 'config', tmp_path / 'scratch', 'ONCE', endpoint.url)
        shell = printed_blocks(environment)[f'dectl {PIPELINE} lambda process run']
        subprocess.run(['bash', '-c', shell], capture_output=True, text=True, env=environment, cwd=CONFIG_DIR, timeout=120)
        invokes = [call for call in endpoint.calls_from('ONCE') if call.operation == 'Invoke']
    assert len(invokes) == 1


def test_the_printed_zip_leaves_out_what_the_deploy_leaves_out(tmp_path):
    """The committed sources hold nothing a deploy skips, so this source holds each kind it skips."""
    source = tmp_path / 'source'
    for relative in ('handler.py', 'pkg/model.py', '__pycache__/handler.cpython-313.pyc', 'pkg/__pycache__/model.pyc', '.git/config'):
        (source / relative).parent.mkdir(parents=True, exist_ok=True)
        (source / relative).write_text(relative)
    config_home = tmp_path / 'config'
    write_tiny_config(config_home, f'lambdas:\n  handler:\n    name: tiny-handler\n    source_dir: {source}')
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
