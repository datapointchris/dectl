from itertools import starmap
from typing import Annotated

import typer
import yaml
from pyclisteno import attach
from pydantic import ValidationError
from pyselfupdate import Config
from pyselfupdate import notify
from pyselfupdate.typercmd import run_update

from dectl.commands.config_cmd import config_app
from dectl.commands.glue import make_glue_app
from dectl.commands.iceberg import make_iceberg_app
from dectl.commands.lambda_ import make_lambda_app
from dectl.commands.monitor import run_monitor
from dectl.commands.release import make_release_app
from dectl.commands.s3 import make_s3_app
from dectl.commands.search import run_search
from dectl.commands.stepfunctions import make_sfn_app
from dectl.config import CONFIG_PATH
from dectl.config import PipelineConfig
from dectl.config import load_config
from dectl.env import active_environment
from dectl.env import set_active_environment
from dectl.output import console
from dectl.output import emit_json
from dectl.output import error
from dectl.output import info
from dectl.pipeline_view import pipeline_to_dict
from dectl.pipeline_view import render_pipeline
from dectl.pipeline_view import resource_types
from dectl.prompt import set_no_input
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
        if pipeline.iceberg_tables:
            resources.append(f'iceberg ({", ".join(pipeline.iceberg_tables)})')
        if pipeline.jenkins and cfg.jenkins:
            resources.append('release (jenkins)')
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
        if pipeline.iceberg_tables:
            pipeline_app.add_typer(make_iceberg_app(name, pipeline, cfg), name='iceberg', rich_help_panel='Resources')
            has_commands = True
        if pipeline.jenkins and cfg.jenkins:
            pipeline_app.add_typer(make_release_app(name, pipeline.jenkins, cfg), name='release', rich_help_panel='Pipeline')
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
                    @papp.command('monitor', rich_help_panel='Pipeline')
                    def monitor_pipeline() -> None:
                        """Tail every configured monitor source for this pipeline as one interleaved stream."""
                        run_monitor(pconfig, gconfig)

                register_monitor_command(pipeline_app, pipeline, cfg)

            app.add_typer(pipeline_app, name=name, rich_help_panel='Pipelines')


# The option-syntax brackets ([--follow], [OPTIONS], ...) would be parsed as rich style tags, so
# these lines are printed with markup disabled; section headers get their style via a style arg.
REFERENCE_INSTANCE_VERBS = [
    'glue    ALIAS  deploy · run [--follow] · logs [RUN_ID] [--follow] · runs [--limit N] [--json]',
    'lambda  ALIAS  deploy [--publish] · run [--payload-file F|-] [--json] · logs [--follow]',
    'sfn     ALIAS  run [--payload-file F|-] [--follow] · logs [ARN] [--follow] · runs [--limit N] [--json]',
    's3      ALIAS  mount · unmount · uri',
    'iceberg ALIAS  snapshots [--limit N] [--json] · history [--limit N] [--json]',
    '               files [SNAPSHOT] [--limit N] [--json] · branches [--json]',
    '               diff [BASE] [TARGET] [--json]',
]
# A lambda flagged `durable` swaps run/logs for the execution-scoped set: its unit of work is the
# durable execution, which spans many invocations, so invocation-shaped verbs answer nothing.
REFERENCE_DURABLE_VERBS = [
    'lambda  ALIAS  deploy [--publish]',
    '               run [--payload-file F|-] [--async] [--name N] [--follow] [--json]',
    '               executions [--status S] [--limit N] [--qualifier Q] [--all-versions] [--json]',
    '               history [EXECUTION] [--follow] [--no-data] [--json]',
    '               logs [EXECUTION] [--follow] [--all] [--context]',
]
REFERENCE_SET_VERBS = [
    's3 export [--prefix STR]',
    'release [--plan] [--follow] · release status [--json] · release logs [--follow]',
    'list [--json] · monitor',
]
REFERENCE_GLOBAL = [
    'reference · env · list [--json] · search KEYWORD [--json] · update [--check]',
    'config  init · show [--json] · path · example · edit · validate',
]


@app.command(rich_help_panel='Global commands')
def reference() -> None:
    """Print the full command grammar, independent of the local config.

    Every resource and its universal verbs, plus the one rule (aliased = one thing,
    unaliased = the whole set). Learn the shape on a fresh machine before authoring config.
    """
    console.print('dectl command grammar', style='bold')
    console.print('  dectl PIPELINE RESOURCE ALIAS VERB [OPTIONS]   — the verb comes last', markup=False)
    console.print('  One rule: acting on one thing takes an alias; the whole set takes none.', markup=False)
    console.print()
    console.print('Instance verbs  (dectl PIPELINE RESOURCE ALIAS VERB)', style='bold cyan')
    for line in REFERENCE_INSTANCE_VERBS:
        console.print(f'  {line}', markup=False)
    console.print()
    console.print('Durable lambda verbs  (a lambda configured durable: true)', style='bold cyan')
    for line in REFERENCE_DURABLE_VERBS:
        console.print(f'  {line}', markup=False)
    console.print()
    console.print('Set / pipeline verbs  (no alias)', style='bold magenta')
    for line in REFERENCE_SET_VERBS:
        console.print(f'  {line}', markup=False)
    console.print()
    console.print('Global', style='bold')
    for line in REFERENCE_GLOBAL:
        console.print(f'  {line}', markup=False)
    console.print()

    if cfg:
        configured = sorted({rtype for p in cfg.pipelines.values() for rtype in resource_types(p)})
        if cfg.jenkins and any(p.jenkins for p in cfg.pipelines.values()):
            configured.append('release')
        info(f'Configured on this machine: {", ".join(configured) or "(none)"}')
    else:
        info('No config on this machine yet — run "dectl config init".')


# Shared by the `update` command and the daily check in the root callback, so the
# notice cannot name a release the update command would not install.
#
# No token is configured because pyselfupdate authenticates itself, running
# `gh auth token` when a request is about to be made. `$GITHUB_TOKEN_COMMAND`
# redirects that or empties it, and it is the user's lever rather than ours.
UPDATE_CONFIG = Config(tool='dectl', owner='datapointchris')


@app.command(rich_help_panel='Global commands')
def update(
    check_only: Annotated[bool, typer.Option('--check', help='Report whether an update is available without installing it')] = False,
) -> None:
    """Update dectl to the latest published GitHub release."""
    run_update(UPDATE_CONFIG, check_only=check_only)


@app.callback(
    invoke_without_command=True,
    epilog=(
        'Examples:\n\n'
        f'dectl {EXAMPLE_PIPELINE} list — show what this pipeline manages\n\n'
        f'dectl {EXAMPLE_PIPELINE} glue JOB run --follow — start a Glue job and tail it\n\n'
        f'dectl {EXAMPLE_PIPELINE} lambda FN deploy --publish — deploy and move the live alias\n\n'
        'dectl reference — the full command grammar\n\n'
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
    no_input: Annotated[
        bool,
        typer.Option('--no-input', help='Never prompt; fail naming the flag that would have answered.'),
    ] = False,
) -> None:
    """[bold]dectl[/bold] — data engineering control for AWS pipelines.

    Commands follow the shape: [bold]dectl PIPELINE RESOURCE ALIAS VERB [OPTIONS][/bold] — the
    verb comes last, so a deploy → run → logs loop on one resource changes only the final word.

    [bold]One rule: aliased vs. set.[/bold] Acting on one thing takes an alias
    ([bold]glue JOB run[/bold], [bold]s3 BUCKET mount[/bold]); acting on the whole set takes none
    ([bold]s3 export[/bold], [bold]release[/bold], [bold]list[/bold], [bold]monitor[/bold]).

    Pipelines and their resources come from your config. Every level is self-documenting: run
    any partial command with no arguments or [bold]--help[/bold] to see what is next. Aliases are
    the short config keys, not full AWS names — run [bold]dectl PIPELINE list[/bold] for the mapping,
    or [bold]dectl reference[/bold] for the whole grammar.

    Config names carry an [bold]{env}[/bold] placeholder; [bold]--env prod[/bold] (or [bold]DECTL_ENV=prod[/bold])
    substitutes it, so one config drives every environment.
    """
    set_active_environment(env, resolve_env_source(ctx))
    set_no_input(no_input)
    # Never raises and never prints an error; the notice is deferred to exit so it lands after the
    # command's own output. `dectl update` is the only place an update failure is reported, and is
    # skipped here because it is about to do the thing the notice would suggest.
    if ctx.invoked_subcommand != 'update':
        notify(UPDATE_CONFIG)
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


# Last line of the module, not after the pipeline loop: the tree is not finished
# until the global commands below it are registered, and a walk that runs earlier
# would publish a grammar missing half of them. It cannot move to a lazier point
# either — the tree does not exist until config is read, which is why enrollment
# is this one call rather than a decorator per command.
attach(app, teaching=True, expanding=True)
