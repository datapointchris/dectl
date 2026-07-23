from itertools import starmap
from pathlib import Path
from typing import Annotated

import typer
import yaml
from pydantic import ValidationError

from dectl.commands.config_cmd import config_app
from dectl.commands.deploy import make_deploy_app
from dectl.commands.glue import make_glue_app
from dectl.commands.lambda_ import make_lambda_app
from dectl.commands.monitor import run_monitor
from dectl.commands.s3 import make_s3_app
from dectl.commands.search import run_search
from dectl.commands.stepfunctions import make_sfn_app
from dectl.config import CONFIG_PATH
from dectl.config import PipelineConfig
from dectl.config import load_config
from dectl.env import active_environment
from dectl.env import set_active_environment
from dectl.output import emit_json
from dectl.output import error
from dectl.output import info
from dectl.output import success
from dectl.pipeline_view import pipeline_to_dict
from dectl.pipeline_view import render_pipeline
from dectl.session import make_session

# no_args_is_help is intentionally omitted: it would short-circuit to help before the callback
# runs, so the callback (invoke_without_command) prints the env banner then the help itself.
app = typer.Typer(name='dectl', invoke_without_command=True)
app.add_typer(config_app, name='config', rich_help_panel='Global commands')


def print_environment_banner() -> None:
    info(f'environment: [bold]{active_environment.name}[/bold]  (from {active_environment.source})')


# Click's ParameterSource enum names -> how a user would refer to that source.
ENV_SOURCE_LABELS = {'COMMANDLINE': '--env', 'ENVIRONMENT': 'DECTL_ENV'}


def resolve_env_source(ctx: typer.Context) -> str:
    source = ctx.get_parameter_source('env')
    source_name = source.name if source is not None else 'DEFAULT'
    # A DEFAULT source means the option default fired — and that default is sourced from the
    # config's `environment` field (falling back to 'dev' only when there is no config at all).
    return ENV_SOURCE_LABELS.get(source_name, 'config' if cfg else 'default')


# A present-but-invalid config must not brick the CLI: without this guard the ValidationError
# would propagate out of import and take down every command, including the `config validate` /
# `config edit` you would use to fix it. Fall back to no config and surface the reason in the
# root callback's banner instead.
try:
    cfg = load_config()
    CONFIG_LOAD_ERROR: str | None = None
except (ValidationError, yaml.YAMLError) as exc:
    cfg = None
    CONFIG_LOAD_ERROR = str(exc)

EXAMPLE_PIPELINE = next(iter(cfg.pipelines), 'my-pipeline') if cfg and cfg.pipelines else 'my-pipeline'
DEFAULT_ENVIRONMENT = cfg.defaults.environment if cfg else 'dev'
if cfg:
    for name, pipeline in cfg.pipelines.items():
        resources = []
        if pipeline.glue_jobs:
            resources.append(f'glue ({", ".join(pipeline.glue_jobs)})')
        if pipeline.lambdas:
            resources.append(f'lambda ({", ".join(pipeline.lambdas)})')
        if pipeline.step_functions:
            resources.append(f'sfn ({", ".join(pipeline.step_functions)})')
        if pipeline.buckets:
            resources.append(f's3 ({", ".join(pipeline.buckets)})')
        if pipeline.jenkins and cfg.jenkins:
            resources.append('deploy (jenkins)')
        summary = ' · '.join(resources) if resources else 'none configured'
        pipeline_app = typer.Typer(
            no_args_is_help=True,
            help=f'[bold]{name}[/bold] pipeline — {summary}',
        )
        has_commands = False
        if pipeline.glue_jobs:
            pipeline_app.add_typer(make_glue_app(name, pipeline, cfg), name='glue', rich_help_panel='Resources')
            has_commands = True
        if pipeline.lambdas:
            pipeline_app.add_typer(make_lambda_app(name, pipeline, cfg), name='lambda', rich_help_panel='Resources')
            has_commands = True
        if pipeline.step_functions:
            pipeline_app.add_typer(make_sfn_app(name, pipeline, cfg), name='sfn', rich_help_panel='Resources')
            has_commands = True
        if pipeline.buckets:
            pipeline_app.add_typer(make_s3_app(name, pipeline, cfg), name='s3', rich_help_panel='Resources')
            has_commands = True
        if pipeline.jenkins and cfg.jenkins:
            pipeline_app.add_typer(make_deploy_app(name, pipeline.jenkins, cfg), name='deploy', rich_help_panel='Resources')
            has_commands = True
        has_monitor = bool(pipeline.monitor.lambdas or pipeline.monitor.step_functions)
        if has_monitor:
            has_commands = True
        if has_commands:

            def _make_list_cmd(papp: typer.Typer, pname: str, pconfig: PipelineConfig):
                @papp.command('list', rich_help_panel='Pipeline')
                def list_resources(
                    as_json: Annotated[bool, typer.Option('--json', help='Emit machine-readable JSON to stdout.')] = False,
                ) -> None:
                    """Show this pipeline's jobs, functions, and buckets with their real AWS names."""
                    if as_json:
                        emit_json(pipeline_to_dict(pname, pconfig))
                    else:
                        render_pipeline(pname, pconfig)

            _make_list_cmd(pipeline_app, name, pipeline)

            if has_monitor:

                def register_monitor_command(papp: typer.Typer, pconfig: PipelineConfig, gconfig) -> None:
                    @papp.command('monitor', rich_help_panel='Info')
                    def monitor_pipeline() -> None:
                        """Tail every configured monitor source for this pipeline as one interleaved stream."""
                        run_monitor(pconfig, gconfig)

                register_monitor_command(pipeline_app, pipeline, cfg)

            app.add_typer(pipeline_app, name=name, rich_help_panel='Pipelines')


@app.command(rich_help_panel='Global commands')
def update() -> None:
    """Reinstall dectl from your local source checkout."""
    import subprocess

    source_dir = Path.home() / 'tools' / 'dectl'
    if not (source_dir / 'pyproject.toml').exists():
        error(f'source not found at {source_dir}')
        raise typer.Exit(1)
    info(f'reinstalling from {source_dir}')
    result = subprocess.run(['uv', 'tool', 'install', '--reinstall', str(source_dir)], capture_output=True, text=True)  # nosec B607
    if result.returncode == 0:
        success('dectl updated')
    else:
        error(result.stderr.strip())
        raise typer.Exit(1)


@app.callback(
    invoke_without_command=True,
    epilog=(
        'Examples:\n\n'
        f'dectl {EXAMPLE_PIPELINE} list — show what this pipeline manages\n\n'
        f'dectl {EXAMPLE_PIPELINE} lambda deploy FUNCTION --publish — deploy and move the alias\n\n'
        f'dectl {EXAMPLE_PIPELINE} glue run JOB — start a Glue job and tail its logs\n\n'
        'dectl search my-bucket — find AWS resources by keyword'
    ),
)
def main(
    ctx: typer.Context,
    env: Annotated[
        str,
        typer.Option(
            '--env',
            envvar='DECTL_ENV',
            help='Environment substituted for {env} in resource names. Priority: --env > DECTL_ENV > config > dev.',
        ),
    ] = DEFAULT_ENVIRONMENT,
) -> None:
    """[bold]dectl[/bold] — data engineering control for AWS pipelines.

    Commands follow the shape: [bold]dectl PIPELINE RESOURCE ACTION [ALIAS] [OPTIONS][/bold]

    Pipelines come from your config (below, under Pipelines). Each pipeline
    exposes the resources it defines — [bold]glue[/bold], [bold]lambda[/bold], and/or [bold]deploy[/bold] (Jenkins).
    Every level is self-documenting: run any partial command with no arguments
    or [bold]--help[/bold] to see what is available next.

    Aliases (the JOB / FUNCTION names) are the short keys from your config, not
    the full AWS resource names — run [bold]dectl PIPELINE list[/bold] to see the mapping.

    Config names carry an [bold]{env}[/bold] placeholder; [bold]--env prod[/bold] (or [bold]DECTL_ENV=prod[/bold])
    substitutes it, so one config drives every environment.
    """
    set_active_environment(env, resolve_env_source(ctx))
    if ctx.invoked_subcommand is None:
        print_environment_banner()
        if CONFIG_LOAD_ERROR:
            error(f'config at {CONFIG_PATH} is invalid — pipelines unavailable.')
            info('run "dectl config validate" to see what is wrong, or "dectl config edit" to fix it')
        print(ctx.get_help())


@app.command(rich_help_panel='Global commands')
def search(
    keyword: Annotated[str, typer.Argument(help='Case-insensitive substring to match against resource names')],
    as_json: Annotated[bool, typer.Option('--json', help='Emit machine-readable JSON to stdout.')] = False,
) -> None:
    """Search AWS by keyword across S3, Lambda, layers, Glue, Step Functions, IAM, and Secrets.

    Scans every configured service in your default region and prints one table
    per service where a name contains the keyword. Useful for finding the real
    AWS name behind a config alias, or checking whether a resource exists.
    """
    if not cfg:
        error('no config loaded — run "dectl config init" first')
        raise typer.Exit(1)
    session = make_session(cfg)
    run_search(session, keyword, cfg.defaults.region, as_json=as_json)


@app.command('env', rich_help_panel='Global commands')
def show_env() -> None:
    """Show the active environment substituted for {env}, and where it was resolved from."""
    print_environment_banner()


@app.command('list', rich_help_panel='Global commands')
def list_all(
    as_json: Annotated[bool, typer.Option('--json', help='Emit machine-readable JSON to stdout.')] = False,
) -> None:
    """List every pipeline with its jobs, functions, and buckets (alias → AWS name)."""
    if not cfg:
        error('no config loaded — run "dectl config init" first')
        raise typer.Exit(1)

    if as_json:
        emit_json(list(starmap(pipeline_to_dict, cfg.pipelines.items())))
        return
    for name, p in cfg.pipelines.items():
        render_pipeline(name, p)
