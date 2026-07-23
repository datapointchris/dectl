from typing import Annotated

import typer
from rich.table import Table

from dectl.config import DectlConfig
from dectl.config import StepFunctionConfig
from dectl.env import render_env_model
from dectl.logs import tail_execution_history
from dectl.output import console
from dectl.output import emit_json
from dectl.output import error
from dectl.output import info
from dectl.payloads import read_payload
from dectl.session import make_session

EXECUTION_STATUS_COLORS = {'SUCCEEDED': 'green', 'FAILED': 'red', 'ABORTED': 'red', 'TIMED_OUT': 'red', 'RUNNING': 'cyan'}


def state_machine_arn(config: DectlConfig, sfn: StepFunctionConfig) -> str:
    return f'arn:aws:states:{config.defaults.region}:{config.defaults.account_id}:stateMachine:{sfn.name}'


def latest_execution_arn(client, arn: str) -> str | None:
    resp = client.list_executions(stateMachineArn=arn, maxResults=1)
    executions = resp.get('executions', [])
    return executions[0]['executionArn'] if executions else None


def make_sfn_machine_app(pipeline_name: str, alias: str, sfn_config: StepFunctionConfig, config: DectlConfig) -> typer.Typer:
    """Build the per-machine sub-app: `dectl PIPELINE sfn <alias> <verb>`."""
    sfn_app = typer.Typer(
        no_args_is_help=True,
        help=f'State machine [bold]{alias}[/bold] → {sfn_config.name}',
    )

    def resolved() -> StepFunctionConfig:
        return render_env_model(sfn_config)

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
        limit: Annotated[int, typer.Option('--limit', '-n', help='Number of executions to show.')] = 10,
        as_json: Annotated[bool, typer.Option('--json', help='Emit machine-readable JSON to stdout.')] = False,
    ) -> None:
        """List recent executions of this state machine with their status and timing."""
        sfn = resolved()
        client = make_session(config).client('stepfunctions')
        executions = client.list_executions(stateMachineArn=state_machine_arn(config, sfn), maxResults=limit).get('executions', [])

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
            make_sfn_machine_app(pipeline_name, alias, sfn_config, config),
            name=alias,
            rich_help_panel='Machines',
        )
    return sfn_app
