"""The aws, curl and mount-s3 lines that make the calls behind each of a pipeline's verbs.

`dectl PIPELINE commands` prints them, so a verb can be run where dectl is not installed, or kept
beside the pipeline as a record of what it calls. Every name is resolved for the active
environment, as the verb resolves it when it runs. Each block runs alone in bash or zsh, and stops
at its first failure.

Names, windows, URIs and qualifiers come from the verb's own constants and renderers. The iceberg
filters, the archive and start-time scripts, and the order of each block's calls are written again
in shell, so a change to a verb's refusals or fallbacks needs the same change in its block. The
tests run each block and its verb against one recording endpoint, in states where the verb refuses
or falls back as well as the one where every check passes, and compare what each sent."""

import json
import re
import shlex
from collections.abc import Callable
from pathlib import Path
from typing import NamedTuple

from dectl.asl import render_definition
from dectl.commands.glue import LOG_START_SLACK_MS
from dectl.commands.glue import script_uri
from dectl.commands.jenkins import job_url_path
from dectl.commands.lambda_ import LOG_WINDOW_BUFFER_MS
from dectl.commands.monitor import build_monitor_sources
from dectl.commands.s3 import MOUNT_BASE
from dectl.commands.s3 import shell_variable_name
from dectl.commands.stepfunctions import alias_arn
from dectl.commands.stepfunctions import parse_rendered
from dectl.commands.stepfunctions import read_definition_template
from dectl.commands.stepfunctions import refuse_template_problems
from dectl.commands.stepfunctions import state_machine_arn
from dectl.config import DectlConfig
from dectl.config import GlueJobConfig
from dectl.config import IcebergTableConfig
from dectl.config import JenkinsJobConfig
from dectl.config import LambdaConfig
from dectl.config import PipelineConfig
from dectl.config import StepFunctionConfig
from dectl.config import pipeline_value_faults
from dectl.config import resolve_from_root
from dectl.durable import VERSION_SWEEP_DEPTH
from dectl.durable import invoke_qualifier
from dectl.durable import is_version
from dectl.env import active_environment
from dectl.env import describe_active_environment
from dectl.env import render_env_model
from dectl.env import substitute_env
from dectl.env import warn_if_environment_had_no_effect
from dectl.iceberg import DIFF_COUNTERS
from dectl.iceberg import METADATA_LOCATION_KEY
from dectl.logs import GLUE_ERROR_LOG_GROUP
from dectl.logs import GLUE_OUTPUT_LOG_GROUP
from dectl.pipeline_view import aws_names_only
from dectl.values import NEVER_DEPLOYED
from dectl.values import bucket_uri
from dectl.values import refuse_unusable_values

# StartLiveTail takes at most this many log groups, so a longer monitor list is several tails.
LIVE_TAIL_GROUP_LIMIT = 10

# What iceberg.py works out from a metadata file, in jq: the current snapshot, each snapshot's
# lineage by parent pointer with a cycle guard, and the summary counters read as integers. Each
# definition uses only those above it, as jq requires.
ICEBERG_DEFINITIONS = {
    'counter': 'def counter($key): .summary[$key] | if . == null then null else (tonumber? // null) end;',
    'operation': 'def operation: .summary.operation // "";',
    'current_id': 'def current_id: ."current-snapshot-id" | if . == null or . < 0 then null else . end;',
    'by_id': 'def by_id: (.snapshots // []) | map({key: (."snapshot-id" | tostring), value: .}) | from_entries;',
    'head': (
        'def head: by_id as $by | $by[current_id | tostring]'
        ' // error("this table has no current snapshot, so nothing has been committed to it");'
    ),
    'lineage': """\
def lineage($start): by_id as $by
  | {next: $start, chain: []}
  | until(.next == null or $by[.next | tostring] == null or (.next as $id | any(.chain[]; ."snapshot-id" == $id));
      .chain += [$by[.next | tostring]] | .next = .chain[-1]."parent-snapshot-id")
  | .chain;""",
    'average_file_bytes': """\
def average_file_bytes: counter("total-files-size") as $total | counter("total-data-files") as $files
  | if $total == null or ($files // 0) == 0 then null else ($total / $files | floor) end;""",
    'snapshot_row': """\
def snapshot_row($refs): ."snapshot-id" as $id | {
  snapshot_id: $id,
  parent_snapshot_id: ."parent-snapshot-id",
  sequence_number: ."sequence-number",
  timestamp_ms: ."timestamp-ms",
  operation: operation,
  schema_id: ."schema-id",
  added_records: counter("added-records"),
  deleted_records: counter("deleted-records"),
  total_records: counter("total-records"),
  added_data_files: counter("added-data-files"),
  deleted_data_files: counter("deleted-data-files"),
  total_data_files: counter("total-data-files"),
  total_delete_files: counter("total-delete-files"),
  total_files_size: counter("total-files-size"),
  average_file_size: average_file_bytes,
  refs: ([$refs | to_entries[] | select(.value."snapshot-id" == $id) | .key] | sort),
  summary: (.summary // {})
};""",
}

# Each iceberg verb's `--json` rows, a timestamp kept as the file's epoch milliseconds and a
# retention as the ref's own fields.
ICEBERG_FILTERS = {
    'snapshots': '(.refs // {}) as $refs | (.snapshots // []) | sort_by(-(."timestamp-ms" // 0)) | map(snapshot_row($refs))',
    'history': """\
(lineage(current_id) | map(."snapshot-id")) as $ancestors | by_id as $by
| (."snapshot-log" // []) | map(."snapshot-id" as $id | {
    timestamp_ms: ."timestamp-ms",
    snapshot_id: $id,
    operation: (($by[$id | tostring] // {}) | operation),
    is_current_ancestor: any($ancestors[]; . == $id)
  })""",
    'files': """\
lineage(head | ."snapshot-id") | map({
  snapshot_id: ."snapshot-id",
  timestamp_ms: ."timestamp-ms",
  operation: operation,
  data_files: counter("total-data-files"),
  delete_files: counter("total-delete-files"),
  position_deletes: counter("total-position-deletes"),
  equality_deletes: counter("total-equality-deletes"),
  records: counter("total-records"),
  total_bytes: counter("total-files-size"),
  average_file_bytes: average_file_bytes
})""",
    'diff': f"""\
(.refs // {{}}) as $refs | by_id as $by | head as $target
| ($by[$target."parent-snapshot-id" | tostring]
    // error("snapshot \\($target."snapshot-id") has no parent in this metadata file, so it is the first commit")) as $base
| {{
    base: ($base | snapshot_row($refs)),
    target: ($target | snapshot_row($refs)),
    linear: true,
    elapsed_seconds: (if $base."timestamp-ms" == null or $target."timestamp-ms" == null then null
      else (($target."timestamp-ms" - $base."timestamp-ms") / 1000 | fabs) end),
    schema_changed: ($base."schema-id" != $target."schema-id"),
    commits: [$target | snapshot_row($refs)],
    counters: [{json.dumps([list(pair) for pair in DIFF_COUNTERS])}[] as [$metric, $key]
      | ($base | counter($key)) as $before | ($target | counter($key)) as $after
      | select($before != null or $after != null)
      | {{metric: $metric, key: $key, before: $before, after: $after,
          change: (if $before == null or $after == null then null else $after - $before end)}}]
  }}""",
    'branches': """\
current_id as $current | by_id as $by
| (.refs // {}) | to_entries | map(($by[.value."snapshot-id" | tostring] // {}) as $snapshot | {
    name: .key,
    type: ((.value.type // "") | ascii_downcase),
    snapshot_id: .value."snapshot-id",
    timestamp_ms: $snapshot."timestamp-ms",
    operation: ($snapshot | operation),
    is_current: (.value."snapshot-id" == $current),
    min_snapshots_to_keep: .value."min-snapshots-to-keep",
    max_snapshot_age_ms: .value."max-snapshot-age-ms",
    max_ref_age_ms: .value."max-ref-age-ms"
  })
| sort_by(.type != "branch", .name)""",
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

# `sweep_executions`' pick: of one `start<TAB>arn` line per version swept, newest first, the
# latest start, the first of a tie. A version holding none printed `None` alone.
NEWEST_EXECUTION_SCRIPT = """\
import datetime, sys
listed, function, depth = sys.argv[1:]
epoch = datetime.datetime(1970, 1, 1, tzinfo=datetime.timezone.utc)
with open(listed) as lines:
    found = [line.rstrip('\\n').split('\\t') for line in lines if '\\t' in line]
if not found:
    sys.exit(f'no durable execution for {function} in its newest {depth} published versions or $LATEST')
print(max(found, key=lambda row: epoch if row[0] == 'None' else datetime.datetime.fromisoformat(row[0]))[1])"""

HEREDOC_OPENER = re.compile(r"<<'(\w+)'(?: &&)?$")


def jq_program(jq_filter: str) -> str:
    """`jq_filter` after the iceberg definitions it reaches, in the order they are defined."""
    used: set[str] = set()
    while True:
        reached = '\n'.join([jq_filter, *(ICEBERG_DEFINITIONS[name] for name in used)])
        more = {name for name in ICEBERG_DEFINITIONS if name not in used and re.search(rf'\b{name}\b', reached)}
        if not more:
            break
        used |= more
    return '\n'.join([*(text for name, text in ICEBERG_DEFINITIONS.items() if name in used), jq_filter])


def indented(lines: list[str]) -> list[str]:
    """`lines` two spaces in, except a heredoc's body and closing delimiter, which are read as written."""
    shifted: list[str] = []
    closing = None
    for line in lines:
        if closing is not None:
            shifted.append(line)
            closing = None if line == closing else closing
            continue
        shifted.append(f'  {line}')
        opener = HEREDOC_OPENER.search(line)
        closing = opener.group(1) if opener else None
    return shifted


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


def chained(*steps: str | list[str]) -> list[str]:
    """Steps joined by `&&`, so a block stops at its first failure, as its verb does.

    Nothing may follow a heredoc's closing delimiter, so a step ending in one carries the `&&` on
    the line that opened it."""
    lines: list[str] = []
    for index, step in enumerate(steps):
        step_lines = [step] if isinstance(step, str) else list(step)
        if index < len(steps) - 1:
            opener = HEREDOC_OPENER.search(step_lines[0])
            joint = 0 if opener and step_lines[-1] == opener.group(1) else -1
            step_lines[joint] += ' &&'
        lines += step_lines
    return lines


def found(variable: str, refusal: str) -> str:
    """A step refusing, as the verb does, where a lookup printed None into `variable`."""
    return f'{{ [ "${variable}" != None ] || {{ echo {q(refusal)} >&2; false; }}; }}'


def heredoc(variable: str, text: str) -> list[str]:
    """A step writing `text` byte for byte to a temporary file named by `variable`.

    A heredoc ends its last line with a newline, so text without one is written by printf."""
    created = f'{variable}=$(mktemp) &&'
    if not text.endswith('\n'):
        return [f'{created} printf %s {q(text)} > "${variable}"']
    delimiter = 'END_OF_FILE'
    while delimiter in text.splitlines():
        delimiter += '_'
    return [f'{created} cat > "${variable}" <<\'{delimiter}\'', *text.removesuffix('\n').split('\n'), delimiter]


def since(printed: str, margin_ms: int) -> list[str]:
    """A step setting `$SINCE` to the time in `$printed`, `margin_ms` earlier."""
    return [f'SINCE=$(python3 - "${printed}" {margin_ms} <<\'PYTHON\'', *SINCE_SCRIPT.split('\n'), 'PYTHON', ')']


def tail(aws: Aws, group: str, *options: str) -> str:
    """`aws logs tail`, reading a group nothing has written to as empty, as the verb's cursor does.

    The step before it sets `$TAIL_ERRORS`."""
    missing = q(f'log group {group} does not exist')
    return (
        f'{{ {aws("logs", "tail", q(group), *options)} 2> "$TAIL_ERRORS" || '
        f'{{ grep -q ResourceNotFoundException "$TAIL_ERRORS" && echo {missing}; }} || '
        '{ cat "$TAIL_ERRORS" >&2; false; }; }'
    )


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
        logs = chained(
            aws.value('RUN_ID', 'JobRuns[0].Id', 'glue', 'get-job-runs', '--job-name', name, '--max-items', '1'),
            found('RUN_ID', 'no recent job runs found'),
            aws.value('STARTED', 'JobRun.StartedOn', 'glue', 'get-job-run', '--job-name', name, '--run-id', '"$RUN_ID"'),
            since('STARTED', LOG_START_SLACK_MS),
            'TAIL_ERRORS=$(mktemp)',
            *(
                tail(aws, group, '--log-stream-name-prefix', '"$RUN_ID"', '--since', '"$SINCE"')
                for group in (GLUE_OUTPUT_LOG_GROUP, GLUE_ERROR_LOG_GROUP)
            ),
        )
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
                    '# dectl reads the job before it uploads, so a job it cannot read gets nothing.',
                    *chained(f'{aws("glue", "get-job", "--job-name", name)} > /dev/null', *uploads),
                    '# Where the definition differs from the config, dectl then confirms and sends UpdateJob.',
                    '# UpdateJob replaces the whole definition, so plan is where that difference shows.',
                ],
            ),
            ShellBlock((*path, 'run'), [aws('glue', 'start-job-run', '--job-name', name)]),
            ShellBlock((*path, 'logs'), logs),
            ShellBlock((*path, 'runs'), [aws('glue', 'get-job-runs', '--job-name', name, '--max-items', '10')]),
        ]
    return blocks


def zip_and_update(pipeline: PipelineConfig, configured: LambdaConfig, fn: LambdaConfig, aws: Aws) -> list[list[str]]:
    """The archive and the code update, as steps."""
    source = resolve_from_root(pipeline, substitute_env(configured.source_dir))
    never = '{' + ', '.join(map(repr, sorted(NEVER_DEPLOYED))) + '}'
    return [
        ['ZIP=$(mktemp -d)/lambda.zip'],
        [f'python3 - "$ZIP" {q(str(source))} <<\'PYTHON\'', *ZIP_SCRIPT.format(never=never).split('\n'), 'PYTHON'],
        [aws('lambda', 'update-function-code', '--function-name', q(fn.name), '--zip-file', '"fileb://$ZIP"')],
    ]


def publish_lambda(fn: LambdaConfig, aws: Aws) -> list[list[str]]:
    name = q(fn.name)
    steps = [
        [aws('lambda', 'wait', 'function-updated-v2', '--function-name', name)],
        [aws.value('VERSION', 'Version', 'lambda', 'publish-version', '--function-name', name)],
    ]
    if fn.live_alias:
        steps.append(
            [aws('lambda', 'update-alias', '--function-name', name, '--name', q(fn.live_alias), '--function-version', '"$VERSION"')]
        )
    return steps


def invoke(fn: LambdaConfig, aws: Aws, qualifier: str | None) -> list[str]:
    """One invocation, its payload printed and its exit non-zero where the function failed."""
    words = ['lambda', 'invoke', '--function-name', q(fn.name)]
    if qualifier:
        words += ['--qualifier', q(qualifier)]
    words += ['--payload', q('{}'), '--cli-binary-format', 'raw-in-base64-out', '--cli-read-timeout', '0', '--output', 'json']
    response = 'printf \'%s\\n\' "$RESPONSE"'
    started = [f'{response} | jq -r {q('"durable execution: \\(.DurableExecutionArn)"')}'] if fn.durable else []
    function_error = q('if .FunctionError then "function returned an error (\\(.FunctionError))\\n" | halt_error(1) else empty end')
    return [
        '# One attempt and no read timeout: a retried invoke runs the function again.',
        *chained(
            'PAYLOAD=$(mktemp)',
            f'RESPONSE=$(AWS_MAX_ATTEMPTS=1 {aws(*words)} "$PAYLOAD")',
            *started,
            'cat "$PAYLOAD"',
            'echo',
            f'{response} | jq -j {function_error}',
        ),
    ]


def newest_execution(fn: LambdaConfig, aws: Aws, listed: str) -> list[list[str]]:
    """`resolve_execution` with no name: the newest on the live version, else across a sweep.

    The live version holds nothing right after `deploy --publish` moves the alias, so the sweep
    is what finds the run before it."""
    name = q(fn.name)
    listing = ('lambda', 'list-durable-executions-by-function', '--function-name', name, '--max-items', '1')
    versions = (
        "{ printf '%s\\n' \"$VERSIONS\" | tr '\\t' '\\n' | grep -Fvx '$LATEST' | "
        f"tail -n {VERSION_SWEEP_DEPTH} | sort -rn; echo '$LATEST'; }} |"
    )
    each_version = aws(
        *listing, '--qualifier', "'{}'", '--query', q('DurableExecutions[0].[StartTimestamp,DurableExecutionArn]'), '--output', 'text'
    )
    sweep = chained(
        aws.value('VERSIONS', 'Versions[].Version', 'lambda', 'list-versions-by-function', '--function-name', name),
        'SWEEP=$(mktemp)',
        [versions, f'  xargs -I \'{{}}\' {each_version} > "$SWEEP"'],
        [
            f'EXECUTION_ARN=$(python3 - "$SWEEP" {name} {VERSION_SWEEP_DEPTH} <<\'PYTHON\'',
            *NEWEST_EXECUTION_SCRIPT.split('\n'),
            'PYTHON',
            ')',
        ],
    )
    return [
        [aws.value('EXECUTION_ARN', 'DurableExecutions[0].DurableExecutionArn', *listing, '--qualifier', listed)],
        ['if [ "$EXECUTION_ARN" = None ]; then', *indented(sweep), 'fi'],
    ]


def lambda_blocks(pipeline_name: str, pipeline: PipelineConfig, aws: Aws) -> list[ShellBlock]:
    blocks = []
    for alias, configured in pipeline.lambdas.items():
        fn: LambdaConfig = render_env_model(configured)
        name = q(fn.name)
        group = f'/aws/lambda/{fn.name}'
        path = (pipeline_name, 'lambda', alias)
        update = zip_and_update(pipeline, configured, fn, aws)
        unmoved = [] if fn.live_alias else ['# no live_alias is configured, so no alias moves']
        blocks += [
            ShellBlock((*path, 'deploy'), chained(*update)),
            ShellBlock((*path, 'deploy', '--publish'), [*chained(*update, *publish_lambda(fn, aws)), *unmoved]),
        ]
        if not fn.durable:
            # From the newest stream's first event, or now for a function that has never run.
            first_event = aws.value(
                'FIRST_EVENT',
                'logStreams[0].firstEventTimestamp',
                'logs',
                'describe-log-streams',
                '--log-group-name',
                q(group),
                '--order-by',
                'LastEventTime',
                '--descending',
                '--max-items',
                '1',
            )
            logs = chained(first_event, since('FIRST_EVENT', 0), 'TAIL_ERRORS=$(mktemp)', tail(aws, group, '--since', '"$SINCE"'))
            blocks += [ShellBlock((*path, 'run'), invoke(fn, aws, None)), ShellBlock((*path, 'logs'), logs)]
            continue

        qualifier = invoke_qualifier(fn)
        if is_version(qualifier):
            versions, listed = [], q(qualifier)
        else:
            versions = [aws.value('VERSION', 'FunctionVersion', 'lambda', 'get-alias', '--function-name', name, '--name', q(qualifier))]
            listed = '"$VERSION"'
        newest: list[str | list[str]] = [*versions, *newest_execution(fn, aws, listed)]
        swept = [
            f'# The newest execution on the live version, or else the newest across its {VERSION_SWEEP_DEPTH} newest',
            '# published versions and $LATEST, as dectl looks.',
        ]
        execution = ('--durable-execution-arn', '"$EXECUTION_ARN"')
        listing = ('lambda', 'list-durable-executions-by-function', '--function-name', name, '--qualifier', listed)
        blocks += [
            ShellBlock((*path, 'run'), invoke(fn, aws, qualifier)),
            ShellBlock((*path, 'executions'), chained(*versions, aws(*listing, '--max-items', '10'))),
            ShellBlock(
                (*path, 'history'),
                [*swept, *chained(*newest, aws('lambda', 'get-durable-execution-history', *execution, '--include-execution-data'))],
            ),
            ShellBlock(
                (*path, 'logs'),
                [
                    *swept,
                    *chained(
                        *newest,
                        aws.value('STARTED', 'StartTimestamp', 'lambda', 'get-durable-execution', *execution),
                        since('STARTED', LOG_WINDOW_BUFFER_MS),
                        'TAIL_ERRORS=$(mktemp)',
                        tail(aws, group, '--filter-pattern', '"\\"$EXECUTION_ARN\\""', '--since', '"$SINCE"'),
                    ),
                ],
            ),
        ]
    return blocks


def rendered_definition(pipeline: PipelineConfig, sfn: StepFunctionConfig, definition_file: str) -> str:
    """The definition `deploy` would send, refused where `deploy` refuses it."""
    source = resolve_from_root(pipeline, substitute_env(definition_file))
    template = read_definition_template(source)
    rendering = render_definition(template, sfn.definition_substitutions)
    if rendering.problems:
        # No live definition is read for a suggestion, because printing the blocks calls no AWS.
        refuse_template_problems(source, template, sfn, rendering.problems, live=None)
    parse_rendered(source, rendering.text)
    return rendering.text


def definition_blocks(
    pipeline_name: str, alias: str, pipeline: PipelineConfig, sfn: StepFunctionConfig, arn: str, aws: Aws
) -> list[ShellBlock]:
    """plan and deploy, with and without --publish, for a machine whose config names a definition file.

    Each runs `prepare`'s refusals in its order before anything writes: the live machine, the
    validation, then the live alias where the verb publishes."""
    assert sfn.definition_file is not None
    path = (pipeline_name, 'sfn', alias)
    text = rendered_definition(pipeline, sfn, sfn.definition_file)
    notes = ['# aws reads file:// as text, so these CRLF line endings reach Step Functions as LF'] if '\r' in text else []
    machine = ('--state-machine-arn', q(arn))
    validate = aws(
        'stepfunctions',
        'validate-state-machine-definition',
        '--definition',
        '"file://$DEFINITION"',
        '--type',
        '"$(jq -r .type "$LIVE")"',
        '--severity',
        'WARNING',
        '--output',
        'json',
    )
    diagnostics = q('.diagnostics[] | "\\(.severity) \\(.code) at \\(.location // "(no location)"): \\(.message)"')
    rejected = q(
        'if .result == "OK" then empty else "Step Functions rejects this definition, so nothing was written\\n" | halt_error(1) end'
    )
    not_validated = q('not validated: this principal lacks states:ValidateStateMachineDefinition, so only the deploy itself checks it')
    checks: list[str | list[str]] = [
        heredoc('DEFINITION', text),
        'LIVE=$(mktemp)',
        f'{aws("stepfunctions", "describe-state-machine", *machine, "--output", "json")} > "$LIVE"',
        'VALIDATION_ERRORS=$(mktemp)',
        [
            f'if VALIDATION=$({validate} 2> "$VALIDATION_ERRORS"); then',
            f'  printf \'%s\\n\' "$VALIDATION" | jq -r {diagnostics} >&2 &&',
            f'  printf \'%s\\n\' "$VALIDATION" | jq -j {rejected}',
            'else',
            f'  grep -q AccessDeniedException "$VALIDATION_ERRORS" && echo {not_validated} >&2 ||',
            '  { cat "$VALIDATION_ERRORS" >&2; false; }',
            'fi',
        ],
    ]
    # Byte for byte, as dectl decides it, since Terraform compares the raw strings.
    unchanged = 'cmp -s <(jq -j .definition "$LIVE") "$DEFINITION"'
    update = aws('stepfunctions', 'update-state-machine', *machine, '--definition', '"file://$DEFINITION"')
    # A difference is what plan exists to show, so it exits 0 on one, as the verb does.
    diff = '{ diff -u <(jq -r .definition "$LIVE" | jq -S .) <(jq -S . "$DEFINITION") || [ $? -eq 1 ]; }'
    publish = ('stepfunctions', 'publish-state-machine-version', *machine)

    if sfn.live_alias:
        live_alias = ('--state-machine-alias-arn', q(alias_arn(arn, sfn.live_alias)))
        routing = aws('stepfunctions', 'describe-state-machine-alias', *live_alias, '--query', 'routingConfiguration', '--output', 'json')
        plan_publish = [*notes, *chained(*checks, routing, diff)]
        alias_read: list[str | list[str]] = [f'{routing} > /dev/null']
        move: list[str | list[str]] = [
            aws(
                'stepfunctions',
                'update-state-machine-alias',
                *live_alias,
                '--routing-configuration',
                '"stateMachineVersionArn=$VERSION_ARN,weight=100"',
            )
        ]
        unmoved = []
    else:
        plan_publish = [*notes, *chained(*checks, diff), '# no live_alias is configured, so deploy --publish moves no alias']
        alias_read, move, unmoved = [], [], ['# no live_alias is configured, so no alias moves']

    deploy_publish = chained(
        *checks,
        *alias_read,
        [
            f'if {unchanged}; then',
            '  REVISION=$(jq -r \'.revisionId // empty\' "$LIVE")',
            'else',
            f'  REVISION=$({update} --query revisionId --output text)',
            'fi',
        ],
        # A machine never updated carries no revisionId, and the publish then names none.
        [
            'if [ -n "$REVISION" ]; then',
            f'  {aws.value("VERSION_ARN", "stateMachineVersionArn", *publish, "--revision-id", '"$REVISION"')}',
            'else',
            f'  {aws.value("VERSION_ARN", "stateMachineVersionArn", *publish)}',
            'fi',
        ],
        *move,
    )
    return [
        ShellBlock((*path, 'plan'), [*notes, *chained(*checks, diff)]),
        ShellBlock((*path, 'plan', '--publish'), plan_publish),
        ShellBlock((*path, 'deploy'), [*notes, *chained(*checks, f'{{ {unchanged} || {update}; }}')]),
        ShellBlock((*path, 'deploy', '--publish'), [*notes, *deploy_publish, *unmoved]),
    ]


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
        logs = chained(
            newest,
            found('EXECUTION_ARN', 'no executions found for this state machine'),
            aws('stepfunctions', 'get-execution-history', '--execution-arn', '"$EXECUTION_ARN"'),
        )
        blocks += [
            ShellBlock((*path, 'run'), [aws('stepfunctions', 'start-execution', *machine, '--input', q('{}'))]),
            ShellBlock((*path, 'logs'), logs),
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
        # Through a file rather than a pipe, so a failed read fails the block instead of handing
        # jq nothing. gunzip -cf passes a file that is not gzipped through unchanged.
        read = [location, 'METADATA=$(mktemp)', f'{aws("s3", "cp", '"$LOCATION"', "-")} > "$METADATA"']
        blocks += [
            ShellBlock(
                (pipeline_name, 'iceberg', alias, verb),
                chained(*read, f'gunzip -cf < "$METADATA" | jq {q(jq_program(jq_filter))}'),
            )
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
    """Every block for one pipeline, in the order `main.py` attaches its resources.

    A pipeline its deploy doors would refuse is refused here too, because a block naming a file
    that does not resolve from here runs against the wrong path."""
    warn_if_environment_had_no_effect(aws_names_only(pipeline))
    refuse_unusable_values(pipeline_value_faults(pipeline_name, pipeline, on_disk=True), exit_code=1)
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
        '# Each block runs alone in bash or zsh, with aws (v2), jq (1.7 or later), python3, curl or mount-s3 where it uses them.',
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
