import json
import math
from collections.abc import Callable
from pathlib import Path
from typing import Annotated
from typing import Any
from typing import NamedTuple
from typing import NoReturn

import typer
import yaml
from botocore.exceptions import BotoCoreError
from botocore.exceptions import ClientError
from rich.markup import escape
from rich.table import Table
from rich.text import Text

from dectl.asl import TEMPLATE_FAULT_WORDING
from dectl.asl import TemplateFault
from dectl.asl import TemplateProblem
from dectl.asl import definition_diff
from dectl.asl import render_definition
from dectl.asl import suggest_substitutions
from dectl.config import DectlConfig
from dectl.config import PipelineConfig
from dectl.config import StepFunctionConfig
from dectl.config import pipeline_value_faults
from dectl.config import resolve_from_root
from dectl.env import active_environment
from dectl.env import generalize_env
from dectl.env import render_env_model
from dectl.env import substitute_env
from dectl.logs import tail_execution_history
from dectl.output import console
from dectl.output import emit_json
from dectl.output import error
from dectl.output import info
from dectl.output import success
from dectl.output import warn
from dectl.payloads import read_payload
from dectl.session import make_session
from dectl.values import refuse_unusable_values

EXECUTION_STATUS_COLORS = {'SUCCEEDED': 'green', 'FAILED': 'red', 'ABORTED': 'red', 'TIMED_OUT': 'red', 'RUNNING': 'cyan'}


def state_machine_arn(config: DectlConfig, sfn: StepFunctionConfig) -> str:
    return f'arn:aws:states:{config.defaults.region}:{config.defaults.account_id}:stateMachine:{sfn.name}'


def alias_arn(machine_arn: str, alias: str) -> str:
    return f'{machine_arn}:{alias}'


def version_number(version_arn: str) -> str:
    return version_arn.rsplit(':', 1)[-1]


def latest_execution_arn(client, arn: str) -> str | None:
    resp = client.list_executions(stateMachineArn=arn, maxResults=1)
    executions = resp.get('executions', [])
    return executions[0]['executionArn'] if executions else None


def read_definition_template(source: Path) -> str:
    # newline='' keeps a CRLF file's line endings, because Terraform renders the bytes as written
    # and a translated line ending is a different definition string.
    with source.open(encoding='utf-8', newline='') as handle:
        return handle.read()


def live_definition_for_suggestions(config: DectlConfig, arn: str) -> str | None:
    """The live definition, or None where it cannot be read, since it only improves a refusal."""
    try:
        return make_session(config).client('stepfunctions').describe_state_machine(stateMachineArn=arn)['definition']
    except (BotoCoreError, ClientError):
        return None


def refuse_template_problems(
    source: Path, template: str, sfn: StepFunctionConfig, problems: list[TemplateProblem], live: str | None
) -> NoReturn:
    """Name every problem and exit, with a substitution for each the live definition can suggest."""
    suggestions = suggest_substitutions(template, sfn.definition_substitutions, live) if live else {}
    error(escape(f'{source} cannot be rendered:'))
    for problem in problems:
        error(escape(f'  line {problem.line}: {problem.fragment} {TEMPLATE_FAULT_WORDING[problem.fault]}'))
        suggestion = suggestions.get(problem.name)
        if suggestion:
            error(escape(f'    the live definition holds {suggestion.value} at {suggestion.location}. As config:'))
            error(escape(f'      {suggested_entry(problem.name, suggestion.value)}'))
    raise typer.Exit(1)


def suggested_entry(name: str, value: str | int | float | bool) -> str:
    """The `definition_substitutions` line a suggestion pastes as, the active env written as {env}.

    The live value names the environment it was deployed to, so pasted as read it would point
    every other environment at this one's resources."""
    note = ''
    if isinstance(value, str):
        general = generalize_env(value)
        if general == value:
            note = f'   (no {active_environment.name} segment in it to write as {{env}})'
        value = general
    return yaml.safe_dump({name: value}, allow_unicode=True, width=math.inf).strip() + note


def parse_rendered(source: Path, text: str) -> Any:
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        error(escape(f'{source} does not render to JSON: {exc.msg} at rendered line {exc.lineno}, column {exc.colno}'))
        raise typer.Exit(1) from None


def describe_live_machine(client, sfn: StepFunctionConfig, arn: str) -> dict:
    try:
        return client.describe_state_machine(stateMachineArn=arn)
    except ClientError as exc:
        if exc.response['Error']['Code'] != 'StateMachineDoesNotExist':
            raise
        error(escape(f'state machine {sfn.name} does not exist ({arn}). deploy replaces a definition and never creates a machine.'))
        raise typer.Exit(1) from None


def refuse_invalid_definition(client, definition: str, machine_type: str) -> None:
    """Refuse a definition Step Functions would reject, and print any warning it raises.

    `UpdateStateMachine` refuses an invalid definition itself. Validating first is what gives
    `plan` the same refusal. A principal without the permission is warned rather than stopped,
    because the write still checks."""
    try:
        response = client.validate_state_machine_definition(definition=definition, type=machine_type, severity='WARNING')
    except ClientError as exc:
        if exc.response['Error']['Code'] != 'AccessDeniedException':
            raise
        warn('not validated: this principal lacks states:ValidateStateMachineDefinition, so only the deploy itself checks it')
        return
    for diagnostic in response.get('diagnostics', []):
        location = diagnostic.get('location') or '(no location)'
        line = escape(f'{diagnostic["severity"]} {diagnostic["code"]} at {location}: {diagnostic["message"]}')
        if diagnostic['severity'] == 'ERROR':
            error(line)
        else:
            warn(line)
    if response['result'] != 'OK':
        error('Step Functions rejects this definition, so nothing was written')
        raise typer.Exit(1)


def current_alias_routing(client, arn: str) -> list[dict]:
    try:
        return client.describe_state_machine_alias(stateMachineAliasArn=arn)['routingConfiguration']
    except ClientError as exc:
        if exc.response['Error']['Code'] != 'ResourceNotFound':
            raise
        error(escape(f'alias {arn} does not exist. Create it, or remove live_alias from the config.'))
        raise typer.Exit(1) from None


def describe_routing(routing: list[dict]) -> str:
    return ', '.join(f'version {version_number(route["stateMachineVersionArn"])} ({route["weight"]}%)' for route in routing)


def print_definition_diff(lines: list[str]) -> None:
    # Text rather than a markup string, because a JSONPath such as `$.items[0]` is a rich tag.
    # Soft-wrapped, so a long ARN stays one line when copied rather than breaking at the width.
    for line in lines:
        if line.startswith(('---', '+++')):
            style = 'bold'
        elif line.startswith('@@'):
            style = 'cyan'
        elif line.startswith('+'):
            style = 'green'
        elif line.startswith('-'):
            style = 'red'
        else:
            style = ''
        console.print(Text(line, style=style), soft_wrap=True)


def publish_and_move_alias(client, sfn: StepFunctionConfig, machine_arn: str, revision_id: str | None) -> None:
    # The revision guards the publish: a definition someone else wrote since dectl read or
    # updated it is refused by Step Functions rather than published under this deploy's name.
    publish_kwargs = {'stateMachineArn': machine_arn}
    if revision_id:
        publish_kwargs['revisionId'] = revision_id
    version_arn = client.publish_state_machine_version(**publish_kwargs)['stateMachineVersionArn']
    version = version_number(version_arn)
    if not sfn.live_alias:
        success(f'published {sfn.name} version {version} (no live_alias configured to move)')
        return
    client.update_state_machine_alias(
        stateMachineAliasArn=alias_arn(machine_arn, sfn.live_alias),
        routingConfiguration=[{'stateMachineVersionArn': version_arn, 'weight': 100}],
    )
    success(f'published {sfn.name} version {version}: alias {sfn.live_alias} -> version {version}')


class PreparedDefinition(NamedTuple):
    """A rendered definition that passed every refusal, beside the live state it was diffed against."""

    client: Any
    sfn: StepFunctionConfig
    machine_arn: str
    text: str
    changed: bool
    revision_id: str | None
    routing: list[dict]


def add_definition_verbs(
    sfn_app: typer.Typer,
    pipeline_name: str,
    alias: str,
    definition_file: str,
    pipeline: PipelineConfig,
    config: DectlConfig,
    resolved: Callable[[], StepFunctionConfig],
) -> None:
    """`plan` and `deploy`, for a machine whose config names the file its definition is rendered from."""

    def prepare(publish: bool) -> PreparedDefinition:
        # Before the refusal, because it is what raises the env-effect warning, as at the lambda
        # and glue doors.
        sfn = resolved()
        refuse_unusable_values(
            pipeline_value_faults(pipeline_name, pipeline, on_disk=True, resource=StepFunctionConfig.RESOURCE, alias=alias),
            exit_code=1,
        )
        # Substituted once, here, because `resolve_from_root` takes a value already rendered.
        source = resolve_from_root(pipeline, substitute_env(definition_file))
        template = read_definition_template(source)
        rendering = render_definition(template, sfn.definition_substitutions)
        machine_arn = state_machine_arn(config, sfn)
        if rendering.problems:
            missing = any(problem.fault is TemplateFault.MISSING_SUBSTITUTION for problem in rendering.problems)
            suggestion_source = live_definition_for_suggestions(config, machine_arn) if missing else None
            refuse_template_problems(source, template, sfn, rendering.problems, suggestion_source)
        rendered = parse_rendered(source, rendering.text)

        # Everything that can refuse runs before the first write, so a refusal never lands
        # between the definition update and the publish.
        client = make_session(config).client('stepfunctions')
        live = describe_live_machine(client, sfn, machine_arn)
        refuse_invalid_definition(client, rendering.text, live['type'])
        routing = current_alias_routing(client, alias_arn(machine_arn, sfn.live_alias)) if publish and sfn.live_alias else []

        # Decided on the bytes, because the AWS provider compares the raw strings: a definition
        # differing only in formatting still shows as `~ definition` in Terraform's next plan.
        # The parsed JSON decides only what the diff says.
        changed = rendering.text != live['definition']
        live_definition = json.loads(live['definition'])
        if not changed:
            info(f'{sfn.name}: definition unchanged')
        elif rendered == live_definition:
            info(f'{sfn.name}: the definition differs in formatting only, with no change to its meaning')
        else:
            print_definition_diff(definition_diff(live_definition, rendered, f'{sfn.name} (live)', str(source)))
        return PreparedDefinition(client, sfn, machine_arn, rendering.text, changed, live.get('revisionId'), routing)

    @sfn_app.command(
        epilog=(
            'Examples:\n\n'
            f'dectl {pipeline_name} sfn {alias} plan — show the diff against the live definition\n\n'
            f'dectl {pipeline_name} sfn {alias} plan --publish — also say which version the live alias would leave'
        ),
    )
    def plan(
        publish: Annotated[
            bool,
            typer.Option('--publish', '-p', help='Also say which version deploy --publish would move the live_alias off.'),
        ] = False,
    ) -> None:
        """Render the definition file and show what deploy would change, writing nothing.

        It refuses every definition deploy refuses, so a clean plan is a deploy that will run."""

        prepared = prepare(publish)
        if not publish:
            return
        sfn = prepared.sfn
        moves = (
            f'move alias {sfn.live_alias} off {describe_routing(prepared.routing)}'
            if sfn.live_alias
            else 'move no alias (live_alias is unset)'
        )
        info(f'deploy --publish would publish a version of this definition and {moves}')

    @sfn_app.command(
        epilog=(
            'Examples:\n\n'
            f'dectl {pipeline_name} sfn {alias} deploy — render the definition file and make it the live definition\n\n'
            f'dectl {pipeline_name} sfn {alias} deploy --publish — also publish a version and move the live alias'
        ),
    )
    def deploy(
        publish: Annotated[
            bool,
            typer.Option('--publish', '-p', help='Publish the definition as a version and move the configured live_alias to it.'),
        ] = False,
    ) -> None:
        """Render the definition file and make it the state machine's definition (starts no execution).

        Only the definition is written: the role, logging and tracing stay as Terraform set them.
        When the rendered definition matches the live one byte for byte, nothing is written. An
        execution started within a few seconds of a deploy may still run the previous definition.
        `plan` shows the same diff and writes nothing.

        `run` starts the latest definition, so it picks up a deploy without --publish. Anything
        starting executions through an alias keeps the old one until --publish moves that alias."""

        prepared = prepare(publish)
        revision_id = prepared.revision_id
        if prepared.changed:
            # The rendered text rather than a re-serialization of it: Terraform compares the live
            # string with its own rendering, so these bytes are what keeps its next plan clean.
            update = prepared.client.update_state_machine(stateMachineArn=prepared.machine_arn, definition=prepared.text)
            revision_id = update['revisionId']
            success(f'updated the definition of {prepared.sfn.name}')
        if publish:
            publish_and_move_alias(prepared.client, prepared.sfn, prepared.machine_arn, revision_id)


def make_sfn_machine_app(
    pipeline_name: str, alias: str, sfn_config: StepFunctionConfig, pipeline: PipelineConfig, config: DectlConfig
) -> typer.Typer:
    """Build the per-machine sub-app: `dectl PIPELINE sfn <alias> <verb>`.

    `plan` and `deploy` are added only for a machine naming a `definition_file`, since they have
    nothing to render without one. The panel shows the config as written, because it is built
    before `--env` is parsed."""
    panel = [f'State machine [bold]{alias}[/bold] → {sfn_config.name}']
    if sfn_config.definition_file is not None:
        panel.append(f'Definition rendered from: {sfn_config.definition_file}')
    if sfn_config.live_alias:
        panel.append(f'deploy --publish moves alias: {sfn_config.live_alias}')
    if len(panel) > 1:
        panel.append(f'As configured. Run "dectl {pipeline_name} list" for the active environment.')
    sfn_app = typer.Typer(no_args_is_help=True, help='\n\n'.join(panel))

    def resolved() -> StepFunctionConfig:
        return render_env_model(sfn_config)

    if sfn_config.definition_file is not None:
        add_definition_verbs(sfn_app, pipeline_name, alias, sfn_config.definition_file, pipeline, config, resolved)

    @sfn_app.command(
        epilog=(
            'Examples:\n\n'
            f'dectl {pipeline_name} sfn {alias} run\n\n'
            f'dectl {pipeline_name} sfn {alias} run --payload-file input.json --follow'
        ),
    )
    def run(
        payload_file: Annotated[
            str | None,
            typer.Option('--payload-file', help='JSON input: a file path, or - for stdin. Defaults to {}.'),
        ] = None,
        follow: Annotated[bool, typer.Option('--follow', '-f', help='Tail the execution history until it finishes.')] = False,
    ) -> None:
        """Start a state machine execution and print its ARN (add --follow to tail its history)."""
        sfn = resolved()
        payload = read_payload(payload_file)
        client = make_session(config).client('stepfunctions')
        arn = state_machine_arn(config, sfn)

        resp = client.start_execution(stateMachineArn=arn, input=payload)
        execution_arn = resp['executionArn']
        info(f'started execution: {execution_arn}')

        if follow:
            tail_execution_history(client, execution_arn, follow=True)

    @sfn_app.command(
        epilog=(
            'Examples:\n\n'
            f'dectl {pipeline_name} sfn {alias} logs — show the most recent execution\n\n'
            f'dectl {pipeline_name} sfn {alias} logs arn:aws:states:...:execution:... --follow'
        ),
    )
    def logs(
        execution: Annotated[str | None, typer.Argument(help='Execution ARN. Defaults to the most recent.')] = None,
        follow: Annotated[bool, typer.Option('--follow', '-f', help='Keep tailing while the execution runs.')] = False,
    ) -> None:
        """Show a state machine execution's history (defaults to the most recent execution)."""
        sfn = resolved()
        client = make_session(config).client('stepfunctions')

        if not execution:
            execution = latest_execution_arn(client, state_machine_arn(config, sfn))
            if not execution:
                error('no executions found for this state machine')
                raise typer.Exit(1)
            info(f'using most recent execution: {execution}')

        tail_execution_history(client, execution, follow=follow)

    @sfn_app.command(epilog=f'Example:\n\ndectl {pipeline_name} sfn {alias} runs --limit 5')
    def runs(
        limit: Annotated[int, typer.Option('--limit', '-n', min=0, help='Number of executions to show.')] = 10,
        as_json: Annotated[bool, typer.Option('--json', help='Emit machine-readable JSON to stdout.')] = False,
    ) -> None:
        """List recent executions of this state machine with their status and timing."""
        sfn = resolved()
        client = make_session(config).client('stepfunctions')
        # Step Functions rejects maxResults=0, so a zero row count is answered here rather
        # than sent.
        executions = (
            []
            if limit == 0
            else client.list_executions(stateMachineArn=state_machine_arn(config, sfn), maxResults=limit).get('executions', [])
        )

        if as_json:
            emit_json(
                [
                    {
                        'name': e.get('name'),
                        'status': e.get('status'),
                        'started': e.get('startDate'),
                        'stopped': e.get('stopDate'),
                    }
                    for e in executions
                ]
            )
            return

        if not executions:
            info('no executions found')
            return
        table = Table(title=f'{alias} executions')
        table.add_column('name')
        table.add_column('status')
        table.add_column('started')
        table.add_column('stopped')
        for execution in executions:
            status = execution['status']
            color = EXECUTION_STATUS_COLORS.get(status, 'white')
            started = execution.get('startDate')
            stopped = execution.get('stopDate')
            table.add_row(
                execution['name'],
                f'[{color}]{status}[/{color}]',
                started.isoformat() if started else '',
                stopped.isoformat() if stopped else '',
            )
        console.print(table)

    return sfn_app


def make_sfn_app(pipeline_name: str, pipeline, config: DectlConfig) -> typer.Typer:
    step_functions = pipeline.step_functions
    alias_list = ', '.join(step_functions.keys()) or '(none configured)'

    sfn_app = typer.Typer(
        no_args_is_help=True,
        help=f'Step Functions in [bold]{pipeline_name}[/bold] — pick a machine, then a verb.\n\nMachines: {alias_list}',
    )
    for alias, sfn_config in step_functions.items():
        sfn_app.add_typer(
            make_sfn_machine_app(pipeline_name, alias, sfn_config, pipeline, config),
            name=alias,
            rich_help_panel='Machines',
        )
    return sfn_app
