from typing import Annotated

import typer
from rich.table import Table

from dectl.config import DectlConfig
from dectl.config import StepFunctionConfig
from dectl.logs import tail_execution_history
from dectl.output import console
from dectl.output import error
from dectl.output import info
from dectl.session import make_session


def state_machine_arn(config: DectlConfig, sfn: StepFunctionConfig) -> str:
    return f'arn:aws:states:{config.defaults.region}:{config.defaults.account_id}:stateMachine:{sfn.name}'


def make_sfn_app(pipeline_name: str, pipeline, config: DectlConfig) -> typer.Typer:
    step_functions = pipeline.step_functions
    alias_list = ', '.join(step_functions.keys()) or '(none configured)'
    example = next(iter(step_functions), 'ALIAS')
    alias_arg_help = f'State machine alias from config. Available: {alias_list}'

    sfn_app = typer.Typer(
        no_args_is_help=True,
        help=(f'Start, watch, and list Step Functions executions in [bold]{pipeline_name}[/bold].\n\nMachines: {alias_list}'),
    )

    def resolve_state_machine(alias: str) -> StepFunctionConfig:
        if alias not in step_functions:
            known = ', '.join(step_functions.keys())
            error(f'unknown state machine "{alias}" for pipeline {pipeline_name}. known: {known}')
            raise typer.Exit(1)
        return step_functions[alias]

    def latest_execution_arn(client, arn: str) -> str | None:
        resp = client.list_executions(stateMachineArn=arn, maxResults=1)
        executions = resp.get('executions', [])
        return executions[0]['executionArn'] if executions else None

    @sfn_app.command(
        epilog=(
            'Examples:\n\n'
            f'dectl {pipeline_name} sfn start {example}\n\n'
            f"""dectl {pipeline_name} sfn start {example} '{{"key": "value"}}' --follow"""
        ),
    )
    def start(
        alias: Annotated[str, typer.Argument(help=alias_arg_help)],
        payload: Annotated[str, typer.Argument(help='JSON input for the execution.')] = '{}',
        follow: Annotated[bool, typer.Option('--follow', '-f', help='Tail the execution history until it finishes.')] = False,
    ) -> None:
        """Start a state machine execution and print its ARN (optionally tail it)."""
        sfn = resolve_state_machine(alias)
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
            f'dectl {pipeline_name} sfn watch {example} — tail the most recent execution\n\n'
            f'dectl {pipeline_name} sfn watch {example} arn:aws:states:...:execution:... — tail a specific one'
        ),
    )
    def watch(
        alias: Annotated[str, typer.Argument(help=alias_arg_help)],
        execution: Annotated[str | None, typer.Argument(help='Execution ARN. Defaults to the most recent.')] = None,
    ) -> None:
        """Tail a state machine execution's history (defaults to the most recent execution)."""
        sfn = resolve_state_machine(alias)
        client = make_session(config).client('stepfunctions')

        if not execution:
            execution = latest_execution_arn(client, state_machine_arn(config, sfn))
            if not execution:
                error('no executions found for this state machine')
                raise typer.Exit(1)
            info(f'using most recent execution: {execution}')

        tail_execution_history(client, execution, follow=True)

    @sfn_app.command('list', epilog=f'Example:\n\ndectl {pipeline_name} sfn list {example}')
    def list_executions(
        alias: Annotated[str, typer.Argument(help=alias_arg_help)],
        limit: Annotated[int, typer.Option('--limit', '-n', help='Number of executions to show.')] = 10,
    ) -> None:
        """List recent executions of a state machine with their status and timing."""
        sfn = resolve_state_machine(alias)
        client = make_session(config).client('stepfunctions')

        resp = client.list_executions(stateMachineArn=state_machine_arn(config, sfn), maxResults=limit)
        executions = resp.get('executions', [])
        if not executions:
            info('no executions found')
            return

        table = Table(title=f'{alias} executions')
        table.add_column('name')
        table.add_column('status')
        table.add_column('started')
        table.add_column('stopped')
        status_colors = {'SUCCEEDED': 'green', 'FAILED': 'red', 'ABORTED': 'red', 'TIMED_OUT': 'red', 'RUNNING': 'cyan'}
        for execution in executions:
            status = execution['status']
            color = status_colors.get(status, 'white')
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
