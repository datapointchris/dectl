"""The aws, curl and mount-s3 lines that make the calls behind each of a pipeline's verbs.

`dectl PIPELINE commands` prints them, so a verb can be run where dectl is not installed, or kept
beside the pipeline as a record of what it calls. Every name is resolved for the active
environment, as the verb resolves it when it runs. Each block runs alone in bash or zsh.

A block is built from the constants and renderers its verb uses, never from a second spelling of
them, so the two cannot drift. The tests run each block and its verb against one recording
endpoint and compare what each sent."""

import shlex
from collections.abc import Callable
from pathlib import Path
from typing import NamedTuple

from dectl.asl import TEMPLATE_FAULT_WORDING
from dectl.asl import render_definition
from dectl.commands.glue import LOG_START_SLACK_MS
from dectl.commands.glue import script_uri
from dectl.commands.jenkins import job_url_path
from dectl.commands.lambda_ import LOG_WINDOW_BUFFER_MS
from dectl.commands.monitor import build_monitor_sources
from dectl.commands.s3 import MOUNT_BASE
from dectl.commands.s3 import shell_variable_name
from dectl.commands.stepfunctions import alias_arn
from dectl.commands.stepfunctions import read_definition_template
from dectl.commands.stepfunctions import state_machine_arn
from dectl.config import DectlConfig
from dectl.config import GlueJobConfig
from dectl.config import IcebergTableConfig
from dectl.config import JenkinsJobConfig
from dectl.config import LambdaConfig
from dectl.config import PipelineConfig
from dectl.config import StepFunctionConfig
from dectl.config import resolve_from_root
from dectl.durable import invoke_qualifier
from dectl.durable import is_version
from dectl.env import active_environment
from dectl.env import describe_active_environment
from dectl.env import render_env_model
from dectl.env import substitute_env
from dectl.iceberg import METADATA_LOCATION_KEY
from dectl.logs import GLUE_ERROR_LOG_GROUP
from dectl.logs import GLUE_OUTPUT_LOG_GROUP
from dectl.values import NEVER_DEPLOYED
from dectl.values import bucket_uri

# StartLiveTail takes at most this many log groups, so a longer monitor list is several tails.
LIVE_TAIL_GROUP_LIMIT = 10

# What each iceberg verb shows, as a jq filter over the metadata file every one of them reads.
ICEBERG_FILTERS = {
    'snapshots': '.snapshots | sort_by(."timestamp-ms") | reverse',
    'history': '."snapshot-log" | reverse',
    'files': (
        '.snapshots | sort_by(."timestamp-ms") | reverse'
        ' | map({id: ."snapshot-id"} + (.summary | {"total-data-files", "total-delete-files", "total-files-size"}))'
    ),
    'diff': '."current-snapshot-id" as $id | .snapshots[] | select(."snapshot-id" == $id) | .summary',
    'branches': '.refs',
}

# `zip_lambda`'s archive: every file under the source at the archive root, minus NEVER_DEPLOYED,
# refused when there is nothing to send.
ZIP_SCRIPT = """\
import pathlib, sys, zipfile
archive, source = sys.argv[1], pathlib.Path(sys.argv[2])
with zipfile.ZipFile(archive, 'w', zipfile.ZIP_DEFLATED) as zf:
    for path in sorted(source.rglob('*')):
        if path.is_file() and not {never} & set(path.relative_to(source).parts):
            zf.write(path, path.relative_to(source))
if not zf.namelist():
    sys.exit(f'{{source}} holds no files to deploy')"""

# Where a verb starts reading logs, for `aws logs tail --since`: a time the aws CLI printed, moved
# back by the verb's own margin. The CLI prints a timestamp as ISO 8601 in local time, a log
# stream's first event as epoch milliseconds, and an absent one as None, which the verb reads as now.
SINCE_SCRIPT = """\
import datetime, sys
printed, margin = sys.argv[1], datetime.timedelta(milliseconds=int(sys.argv[2]))
if printed == 'None':
    start = datetime.datetime.now(datetime.timezone.utc)
elif printed.isdigit():
    start = datetime.datetime(1970, 1, 1, tzinfo=datetime.timezone.utc) + datetime.timedelta(milliseconds=int(printed))
else:
    start = datetime.datetime.fromisoformat(printed)
print((start - margin).isoformat())"""


class ShellBlock(NamedTuple):
    """One dectl command, without `dectl`, and the lines that make the calls it makes."""

    command: tuple[str, ...]
    lines: list[str]


def q(value: str) -> str:
    return shlex.quote(value)


def double_quoted(text: str) -> str:
    """`text` as the inside of a double-quoted shell word, so a `$NAME` beside it still expands."""
    return ''.join(f'\\{char}' if char in '$`"\\' else char for char in text)


class Aws:
    """The aws CLI line every block writes: the session's region always, its profile when set."""

    def __init__(self, config: DectlConfig) -> None:
        self.suffix = f' --region {q(config.defaults.region)}'
        if config.defaults.aws_profile:
            self.suffix += f' --profile {q(config.defaults.aws_profile)}'

    def __call__(self, *words: str) -> str:
        return f'aws {" ".join(words)}{self.suffix}'

    def value(self, variable: str, query: str, *words: str) -> str:
        """An assignment holding one field of a call's response."""
        return f'{variable}=$({self(*words, "--query", q(query), "--output", "text")})'


def heredoc(variable: str, text: str) -> list[str]:
    """Lines writing `text` byte for byte to a temporary file named by `variable`.

    A heredoc ends its last line with a newline, so text without one is written by printf."""
    lines = [f'{variable}=$(mktemp)']
    if not text.endswith('\n'):
        return [*lines, f'printf %s {q(text)} > "${variable}"']
    delimiter = 'END_OF_FILE'
    while delimiter in text.splitlines():
        delimiter += '_'
    return [*lines, f'cat > "${variable}" <<\'{delimiter}\'', *text.removesuffix('\n').split('\n'), delimiter]


def since(printed: str, margin_ms: int) -> list[str]:
    """Lines setting `$SINCE` to the time in `$printed`, `margin_ms` earlier."""
    return [f'SINCE=$(python3 - "${printed}" {margin_ms} <<\'PYTHON\'', *SINCE_SCRIPT.split('\n'), 'PYTHON', ')']


def glue_blocks(pipeline_name: str, pipeline: PipelineConfig, aws: Aws) -> list[ShellBlock]:
    blocks = []
    for alias, configured in pipeline.glue_jobs.items():
        job: GlueJobConfig = render_env_model(configured)
        name = q(job.name)
        path = (pipeline_name, 'glue', alias)
        # Substituted once, as `resolve_scripts` does, for both the file and the key.
        scripts = [substitute_env(script) for script in configured.scripts]
        # No guessed content type, because dectl's upload sends none.
        uploads = [
            aws('s3', 'cp', q(str(resolve_from_root(pipeline, script))), q(script_uri(job, script)), '--no-guess-mime-type')
            for script in scripts
        ]
        logs = [
            aws.value('RUN_ID', 'JobRuns[0].Id', 'glue', 'get-job-runs', '--job-name', name, '--max-items', '1'),
            aws.value('STARTED', 'JobRun.StartedOn', 'glue', 'get-job-run', '--job-name', name, '--run-id', '"$RUN_ID"'),
            *since('STARTED', LOG_START_SLACK_MS),
            *(
                aws('logs', 'tail', q(group), '--log-stream-name-prefix', '"$RUN_ID"', '--since', '"$SINCE"')
                for group in (GLUE_OUTPUT_LOG_GROUP, GLUE_ERROR_LOG_GROUP)
            ),
        ]
        blocks += [
            ShellBlock(
                (*path, 'plan'),
                [
                    aws('glue', 'get-job', '--job-name', name),
                    '# dectl compares this definition with the role, arguments, connections and sizing the',
                    '# config names, and shows what deploy would change.',
                ],
            ),
            ShellBlock(
                (*path, 'deploy'),
                [
                    *uploads,
                    '# Where the definition differs from the config, dectl then confirms and sends UpdateJob.',
                    '# UpdateJob replaces the whole definition, so plan is where that difference shows.',
                ],
            ),
            ShellBlock((*path, 'run'), [aws('glue', 'start-job-run', '--job-name', name)]),
            ShellBlock((*path, 'logs'), logs),
            ShellBlock((*path, 'runs'), [aws('glue', 'get-job-runs', '--job-name', name, '--max-items', '10')]),
        ]
    return blocks


def zip_and_update(pipeline: PipelineConfig, configured: LambdaConfig, fn: LambdaConfig, aws: Aws) -> list[str]:
    """The archive and the code update, the update run only when the archive was built."""
    source = resolve_from_root(pipeline, substitute_env(configured.source_dir))
    never = '{' + ', '.join(map(repr, sorted(NEVER_DEPLOYED))) + '}'
    update = aws('lambda', 'update-function-code', '--function-name', q(fn.name), '--zip-file', '"fileb://$ZIP"')
    return [
        'ZIP=$(mktemp -d)/lambda.zip',
        f'python3 - "$ZIP" {q(str(source))} <<\'PYTHON\' &&',
        *ZIP_SCRIPT.format(never=never).split('\n'),
        'PYTHON',
        f'  {update}',
    ]


def publish_lambda(fn: LambdaConfig, aws: Aws) -> list[str]:
    name = q(fn.name)
    lines = [
        f'{aws("lambda", "wait", "function-updated-v2", "--function-name", name)} &&',
        aws.value('VERSION', 'Version', 'lambda', 'publish-version', '--function-name', name),
    ]
    if not fn.live_alias:
        return [*lines, '# no live_alias is configured, so no alias moves']
    lines[-1] += ' &&'
    return [*lines, aws('lambda', 'update-alias', '--function-name', name, '--name', q(fn.live_alias), '--function-version', '"$VERSION"')]


def invoke(fn: LambdaConfig, aws: Aws, qualifier: str | None) -> list[str]:
    words = ['lambda', 'invoke', '--function-name', q(fn.name)]
    if qualifier:
        words += ['--qualifier', q(qualifier)]
    words += ['--payload', q('{}'), '--cli-binary-format', 'raw-in-base64-out', '--cli-read-timeout', '0']
    return [
        '# One attempt and no read timeout: a retried invoke runs the function again.',
        f'AWS_MAX_ATTEMPTS=1 {aws(*words)} /dev/stdout',
    ]


def lambda_blocks(pipeline_name: str, pipeline: PipelineConfig, aws: Aws) -> list[ShellBlock]:
    blocks = []
    for alias, configured in pipeline.lambdas.items():
        fn: LambdaConfig = render_env_model(configured)
        name = q(fn.name)
        group = q(f'/aws/lambda/{fn.name}')
        path = (pipeline_name, 'lambda', alias)
        update = zip_and_update(pipeline, configured, fn, aws)
        blocks += [
            ShellBlock((*path, 'deploy'), update),
            ShellBlock((*path, 'deploy', '--publish'), [*update[:-1], f'{update[-1]} &&', *publish_lambda(fn, aws)]),
        ]
        if not fn.durable:
            # From the newest stream's first event, or now for a function that has never run.
            first_event = aws.value(
                'FIRST_EVENT',
                'logStreams[0].firstEventTimestamp',
                'logs',
                'describe-log-streams',
                '--log-group-name',
                group,
                '--order-by',
                'LastEventTime',
                '--descending',
                '--max-items',
                '1',
            )
            blocks += [
                ShellBlock((*path, 'run'), invoke(fn, aws, None)),
                ShellBlock((*path, 'logs'), [first_event, *since('FIRST_EVENT', 0), aws('logs', 'tail', group, '--since', '"$SINCE"')]),
            ]
            continue

        qualifier = invoke_qualifier(fn)
        if is_version(qualifier):
            versions, listed = [], q(qualifier)
        else:
            versions = [aws.value('VERSION', 'FunctionVersion', 'lambda', 'get-alias', '--function-name', name, '--name', q(qualifier))]
            listed = '"$VERSION"'
        listing = ('lambda', 'list-durable-executions-by-function', '--function-name', name, '--qualifier', listed)
        newest = [*versions, aws.value('EXECUTION_ARN', 'DurableExecutions[0].DurableExecutionArn', *listing, '--max-items', '1')]
        execution = ('--durable-execution-arn', '"$EXECUTION_ARN"')
        blocks += [
            ShellBlock((*path, 'run'), invoke(fn, aws, qualifier)),
            ShellBlock((*path, 'executions'), [*versions, aws(*listing, '--max-items', '10')]),
            ShellBlock(
                (*path, 'history'), [*newest, aws('lambda', 'get-durable-execution-history', *execution, '--include-execution-data')]
            ),
            ShellBlock(
                (*path, 'logs'),
                [
                    *newest,
                    aws.value('STARTED', 'StartTimestamp', 'lambda', 'get-durable-execution', *execution),
                    *since('STARTED', LOG_WINDOW_BUFFER_MS),
                    aws('logs', 'tail', group, '--filter-pattern', '"\\"$EXECUTION_ARN\\""', '--since', '"$SINCE"'),
                ],
            ),
        ]
    return blocks


def rendered_definition(pipeline: PipelineConfig, sfn: StepFunctionConfig, definition_file: str) -> list[str]:
    """The lines writing the rendered definition to `$DEFINITION`, or comments saying why it cannot be."""
    source = resolve_from_root(pipeline, substitute_env(definition_file))
    try:
        template = read_definition_template(source)
    except OSError as exc:
        return [f'# {source} cannot be read ({exc.strerror}), so there is no definition to send']
    rendering = render_definition(template, sfn.definition_substitutions)
    if rendering.problems:
        return [f'# {source} cannot be rendered:'] + [
            f'#   line {problem.line}: {problem.fragment} {TEMPLATE_FAULT_WORDING[problem.fault]}' for problem in rendering.problems
        ]
    lines = heredoc('DEFINITION', rendering.text)
    if '\r' in rendering.text:
        lines.append('# aws reads file:// as text, so these CRLF line endings reach Step Functions as LF')
    return lines


def definition_blocks(
    pipeline_name: str, alias: str, pipeline: PipelineConfig, sfn: StepFunctionConfig, arn: str, aws: Aws
) -> list[ShellBlock]:
    """plan and deploy, with and without --publish, for a machine whose config names a definition file."""
    assert sfn.definition_file is not None
    path = (pipeline_name, 'sfn', alias)
    verbs = [(*path, 'plan'), (*path, 'plan', '--publish'), (*path, 'deploy'), (*path, 'deploy', '--publish')]
    definition = rendered_definition(pipeline, sfn, sfn.definition_file)
    if not definition[0].startswith('DEFINITION='):
        return [ShellBlock(command, definition) for command in verbs]

    machine = ('--state-machine-arn', q(arn))
    read_live = [*definition, 'LIVE=$(mktemp)', f'{aws("stepfunctions", "describe-state-machine", *machine, "--output", "json")} > "$LIVE"']
    validate = aws(
        'stepfunctions',
        'validate-state-machine-definition',
        '--definition',
        '"file://$DEFINITION"',
        '--type',
        '"$(jq -r .type "$LIVE")"',
        '--severity',
        'WARNING',
    )
    plan = [*read_live, validate, 'diff <(jq -r .definition "$LIVE" | jq -S .) <(jq -S . "$DEFINITION")']
    # Byte for byte, as dectl decides it, since Terraform compares the raw strings.
    unchanged = 'cmp -s <(jq -j .definition "$LIVE") "$DEFINITION"'
    update = aws('stepfunctions', 'update-state-machine', *machine, '--definition', '"file://$DEFINITION"')
    publish = ('stepfunctions', 'publish-state-machine-version', *machine)

    if sfn.live_alias:
        live_alias = ('--state-machine-alias-arn', q(alias_arn(arn, sfn.live_alias)))
        plan_publish = [*plan, aws('stepfunctions', 'describe-state-machine-alias', *live_alias, '--query', 'routingConfiguration')]
        move = [
            aws(
                'stepfunctions',
                'update-state-machine-alias',
                *live_alias,
                '--routing-configuration',
                '"stateMachineVersionArn=$VERSION_ARN,weight=100"',
            )
        ]
    else:
        plan_publish = [*plan, '# no live_alias is configured, so deploy --publish moves no alias']
        move = ['# no live_alias is configured, so no alias moves']

    deploy_publish = [
        *read_live,
        f'if {unchanged}; then',
        '  REVISION=$(jq -r \'.revisionId // empty\' "$LIVE")',
        'else',
        f'  REVISION=$({update} --query revisionId --output text)',
        'fi &&',
        # A machine never updated carries no revisionId, and the publish then names none.
        'if [ -n "$REVISION" ]; then',
        f'  {aws.value("VERSION_ARN", "stateMachineVersionArn", *publish, "--revision-id", '"$REVISION"')}',
        'else',
        f'  {aws.value("VERSION_ARN", "stateMachineVersionArn", *publish)}',
        'fi' + (' &&' if sfn.live_alias else ''),
        *move,
    ]
    blocks = [plan, plan_publish, [*read_live, f'{unchanged} || {update}'], deploy_publish]
    return [ShellBlock(command, lines) for command, lines in zip(verbs, blocks, strict=True)]


def sfn_blocks(pipeline_name: str, pipeline: PipelineConfig, config: DectlConfig, aws: Aws) -> list[ShellBlock]:
    blocks = []
    for alias, configured in pipeline.step_functions.items():
        sfn: StepFunctionConfig = render_env_model(configured)
        arn = state_machine_arn(config, sfn)
        machine = ('--state-machine-arn', q(arn))
        path = (pipeline_name, 'sfn', alias)
        if sfn.definition_file is not None:
            blocks += definition_blocks(pipeline_name, alias, pipeline, sfn, arn, aws)
        newest = aws.value('EXECUTION_ARN', 'executions[0].executionArn', 'stepfunctions', 'list-executions', *machine, '--max-items', '1')
        blocks += [
            ShellBlock((*path, 'run'), [aws('stepfunctions', 'start-execution', *machine, '--input', q('{}'))]),
            ShellBlock((*path, 'logs'), [newest, aws('stepfunctions', 'get-execution-history', '--execution-arn', '"$EXECUTION_ARN"')]),
            ShellBlock((*path, 'runs'), [aws('stepfunctions', 'list-executions', *machine, '--max-items', '10')]),
        ]
    return blocks


def s3_blocks(pipeline_name: str, pipeline: PipelineConfig, config: DectlConfig) -> list[ShellBlock]:
    buckets = {alias: substitute_env(bucket) for alias, bucket in pipeline.buckets.items()}
    exports = [f"export {shell_variable_name(pipeline_name, alias)}='{bucket_uri(bucket)}'" for alias, bucket in buckets.items()]
    blocks = [ShellBlock((pipeline_name, 's3', 'export'), exports)]
    options = ''
    if config.defaults.aws_profile:
        options += f' --profile {q(config.defaults.aws_profile)}'
    if config.defaults.region:
        options += f' --region {q(config.defaults.region)}'
    base = MOUNT_BASE.relative_to(Path.home())
    for alias, bucket in buckets.items():
        mountpoint = f'"$HOME/{double_quoted(str(base / pipeline_name / alias))}"'
        path = (pipeline_name, 's3', alias)
        blocks += [
            ShellBlock((*path, 'uri'), [f'echo {q(bucket_uri(bucket))}']),
            ShellBlock((*path, 'mount'), [f'mkdir -p {mountpoint} && mount-s3 {q(bucket)} {mountpoint}{options}']),
            ShellBlock((*path, 'unmount'), [f'umount {mountpoint}']),
        ]
    return blocks


def iceberg_blocks(pipeline_name: str, pipeline: PipelineConfig, aws: Aws) -> list[ShellBlock]:
    blocks = []
    for alias, configured in pipeline.iceberg_tables.items():
        table: IcebergTableConfig = render_env_model(configured)
        location = aws.value(
            'LOCATION',
            f'Table.Parameters.{METADATA_LOCATION_KEY}',
            'glue',
            'get-table',
            '--database-name',
            q(table.database),
            '--name',
            q(table.table),
        )
        # gunzip -cf passes a file that is not gzipped through unchanged.
        read = f'{aws("s3", "cp", '"$LOCATION"', "-")} | gunzip -cf'
        blocks += [
            ShellBlock((pipeline_name, 'iceberg', alias, verb), [location, f'{read} | jq {q(jq_filter)}'])
            for verb, jq_filter in ICEBERG_FILTERS.items()
        ]
    return blocks


def jenkins_credential(value: str, secret: bool) -> tuple[str, str | None]:
    """A credential as the inside of a double-quoted word, and a note where the config holds the secret itself.

    `${NAME}` is the variable dectl reads, and it is printed as one. A literal user is printed as
    written. A literal token never is."""
    if value.startswith('${') and value.endswith('}'):
        return f'${value[2:-1]}', None
    if not secret:
        return double_quoted(value), None
    return '$JENKINS_TOKEN', '# the config writes the Jenkins token itself, which this never prints: export JENKINS_TOKEN first'


def jenkins_blocks(pipeline_name: str, configured: JenkinsJobConfig, config: DectlConfig) -> list[ShellBlock]:
    assert config.jenkins is not None
    job: JenkinsJobConfig = render_env_model(configured)
    user, _ = jenkins_credential(config.jenkins.user, secret=False)
    token, note = jenkins_credential(config.jenkins.token, secret=True)
    notes = [note] if note else []
    job_url = f'{config.jenkins.url.rstrip("/")}/{job_url_path(job).lstrip("/")}'

    def curl(path: str, *options: str) -> list[str]:
        url = q(f'{job_url}/{path}')
        return [*notes, f'curl --silent --show-error --fail-with-body --insecure --user "{user}:{token}" {" ".join([*options, url])}']

    blocks = [
        ShellBlock(
            (pipeline_name, 'jenkins', action),
            curl('buildWithParameters', *(f'--data-urlencode {q(f"{name}={value}")}' for name, value in {**job.parameters, **own}.items())),
        )
        for action, own in job.actions.items()
    ]
    return [
        *blocks,
        ShellBlock((pipeline_name, 'jenkins', 'status'), curl('lastBuild/api/json')),
        ShellBlock((pipeline_name, 'jenkins', 'logs'), curl('lastBuild/logText/progressiveText?start=0')),
    ]


def monitor_block(pipeline_name: str, pipeline: PipelineConfig, config: DectlConfig, aws: Aws) -> ShellBlock:
    sources, warnings = build_monitor_sources(pipeline)
    arns = [f'arn:aws:logs:{config.defaults.region}:{config.defaults.account_id}:log-group:{group}' for _, group in sources]
    tails = [
        aws('logs', 'start-live-tail', '--log-group-identifiers', *map(q, arns[start : start + LIVE_TAIL_GROUP_LIMIT]))
        for start in range(0, len(arns), LIVE_TAIL_GROUP_LIMIT)
    ]
    if not tails:
        tails = ['# nothing to monitor: no entry under monitor names a configured lambda or a state machine with a log_group']
    elif len(tails) > 1:
        tails = [f'{tail} &' for tail in tails] + ['wait']
    return ShellBlock((pipeline_name, 'monitor'), [f'# {warning}' for warning in warnings] + tails)


def pipeline_shell_blocks(pipeline_name: str, pipeline: PipelineConfig, config: DectlConfig) -> list[ShellBlock]:
    """Every block for one pipeline, in the order `main.py` attaches its resources."""
    aws = Aws(config)
    builders: list[Callable[[], list[ShellBlock]]] = [
        lambda: glue_blocks(pipeline_name, pipeline, aws),
        lambda: lambda_blocks(pipeline_name, pipeline, aws),
        lambda: sfn_blocks(pipeline_name, pipeline, config, aws),
        lambda: s3_blocks(pipeline_name, pipeline, config) if pipeline.buckets else [],
        lambda: iceberg_blocks(pipeline_name, pipeline, aws),
        lambda: jenkins_blocks(pipeline_name, pipeline.jenkins, config) if pipeline.jenkins and config.jenkins else [],
        lambda: (
            [monitor_block(pipeline_name, pipeline, config, aws)] if pipeline.monitor.lambdas or pipeline.monitor.step_functions else []
        ),
    ]
    return [block for build in builders for block in build()]


def render_shell_blocks(pipeline_name: str, blocks: list[ShellBlock]) -> str:
    header = [
        f'# The calls behind every dectl {pipeline_name} verb, for environment {describe_active_environment()}.',
        '# Each block runs alone in bash or zsh, with aws (v2), jq, python3, curl or mount-s3 where it uses them.',
        '# Pasted into an interactive zsh, a # line runs as a command unless `setopt interactivecomments` is on.',
    ]
    sections = ['\n'.join([f'# {dectl_command(block)}', *block.lines]) for block in blocks]
    return '\n\n'.join(['\n'.join(header), *sections]) + '\n'


def shell_blocks_to_dict(pipeline_name: str, blocks: list[ShellBlock]) -> dict:
    return {
        'pipeline': pipeline_name,
        'environment': active_environment.name,
        'commands': [{'command': dectl_command(block), 'shell': '\n'.join(block.lines) + '\n'} for block in blocks],
    }


def dectl_command(block: ShellBlock) -> str:
    return ' '.join(['dectl', *block.command])
