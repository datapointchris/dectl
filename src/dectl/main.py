import importlib.metadata
import json
from itertools import starmap
from typing import TYPE_CHECKING
from typing import Annotated
from typing import Any

import typer
from botocore.exceptions import BotoCoreError
from botocore.exceptions import ClientError
from rich.markup import escape
from typer.core import TyperGroup

from dectl.commands.config_cmd import config_app
from dectl.commands.jenkins import JenkinsError
from dectl.commands.monitor import run_monitor
from dectl.commands.search import SEARCHERS
from dectl.commands.search import run_search
from dectl.config import CONFIG_PATH
from dectl.config import DectlConfig
from dectl.config import Defaults
from dectl.config import PipelineConfig
from dectl.config import loaded_config
from dectl.config import report_config_error
from dectl.env import active_environment
from dectl.env import set_active_environment
from dectl.output import console
from dectl.output import emit_json
from dectl.output import error
from dectl.output import info
from dectl.output import stderr_console
from dectl.pipeline_view import pipeline_to_dict
from dectl.pipeline_view import render_pipeline
from dectl.prompt import set_no_input
from dectl.resource_kinds import RESOURCE_KINDS
from dectl.resource_kinds import attached_kinds
from dectl.session import installed_version
from dectl.session import make_session

if TYPE_CHECKING:
    import pyselfupdate

# A present-but-invalid config must not brick the CLI: raised here, the ValidationError would
# propagate out of import and take down every command, including the `config validate` /
# `config edit` you would use to fix it. So `loaded_config` returns the failure rather than raising
# it, and the reason surfaces wherever the missing pipelines are noticed.
#
# The exception itself is kept, not str(exc): every reporting site renders it through
# report_config_error, which walks exc.errors() for the location and the rejected value.
cfg, CONFIG_ERROR = loaded_config()


def require_config() -> DectlConfig:
    """Refuse with the reason there are no pipelines, and never with the wrong one.

    `cfg is None` covers two states that need opposite answers — no config file, and a config
    file that failed to load. Naming the second as the first sends the reader to `config init`,
    which refuses because the file it would write is already there. `CONFIG_ERROR` is what
    separates them, so it is consulted before `cfg`.

    Returning the config is what keeps a caller from reading the global after the guard, where
    the type is `DectlConfig | None` and every use needs a narrowing it cannot get.
    """
    if CONFIG_ERROR is not None:
        report_config_error(CONFIG_ERROR)
        raise typer.Exit(1)
    if cfg is None:
        error(f'no config found at {CONFIG_PATH} — run "dectl config init" first')
        raise typer.Exit(1)
    return cfg


class DectlGroup(TyperGroup):
    """The root group: the global commands, then a `PipelineGroup` for each attached pipeline.

    It also decides how a failure no command handled reaches the reader."""

    def __init__(self, **attrs: Any) -> None:
        super().__init__(**attrs)
        if cfg is not None:
            for name, pipeline in cfg.pipelines.items():
                if pipeline_has_commands(pipeline, cfg):
                    self.add_command(PipelineGroup(name, pipeline, cfg))

    # `Any` because typer vendors click privately as `typer._click`, and those are the types
    # this signature has to match. `click` itself is not a dectl dependency and would be the
    # wrong hierarchy anyway; `typer.Context` is a subclass of the vendored one, so naming it
    # here narrows a parameter the base widens.
    def get_command(self, ctx: Any, cmd_name: str) -> Any:
        """Report the config failure where an unknown pipeline name is what the reader typed.

        Every pipeline is a subcommand assembled from config, so a config that does not load
        takes the whole pipeline tree with it. Click can only report the name as unknown, which
        sends the reader after a typo in a command they have run for months. The substitution
        holds only while there is no loaded config to have contained the name — with one loaded,
        an unknown name really is unknown and Click's usage error is the right answer."""
        command = super().get_command(ctx, cmd_name)
        if command is None and cfg is None:
            require_config()
        return command

    def invoke(self, ctx: Any) -> Any:
        """End on the remote's own sentence where AWS or Jenkins refused and no command said more.

        botocore's message names the operation, the error code and the resource, which is all a
        traceback through botocore's frames would add. A `JenkinsError` names the request, and
        the status or transport failure it met. A command that knows what a refusal means
        catches it first and says that instead."""
        try:
            return super().invoke(ctx)
        except (BotoCoreError, ClientError, JenkinsError) as exc:
            error(escape(str(exc)))
            raise typer.Exit(1) from exc


# no_args_is_help is intentionally omitted: it would short-circuit to help before the callback
# runs, so the callback (invoke_without_command) prints the env banner then the help itself.
app = typer.Typer(name='dectl', cls=DectlGroup, invoke_without_command=True)
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


DEFAULT_ENVIRONMENT = cfg.defaults.environment if cfg else 'dev'


def pipeline_summary(pipeline: PipelineConfig, config: DectlConfig) -> str:
    """Each resource the pipeline has a command for, with its aliases, as the root help lists it."""
    resources = [kind.summary(pipeline) for kind in attached_kinds(pipeline, config)]
    return ' · '.join(resources) if resources else 'none configured'


def pipeline_has_commands(pipeline: PipelineConfig, config: DectlConfig) -> bool:
    """Whether the pipeline configures a resource or a monitor, without which it has no command."""
    return bool(attached_kinds(pipeline, config)) or bool(pipeline.monitor.selected_aliases())


def make_pipeline_app(name: str, pipeline: PipelineConfig, config: DectlConfig) -> typer.Typer:
    """The tree under one pipeline's name: a sub-app per configured resource, then `list` and `monitor`."""
    pipeline_app = typer.Typer()
    for kind in attached_kinds(pipeline, config):
        pipeline_app.add_typer(kind.make_app(name, pipeline, config), name=kind.word, rich_help_panel=kind.panel)

    @pipeline_app.command('list', rich_help_panel='Pipeline')
    def list_resources(
        as_json: Annotated[bool, typer.Option('--json', help='Emit machine-readable JSON to stdout.')] = False,
    ) -> None:
        """Show this pipeline's jobs, functions, and buckets with their real AWS names."""
        if as_json:
            emit_json(pipeline_to_dict(name, pipeline))
        else:
            render_pipeline(name, pipeline)

    if pipeline.monitor.selected_aliases():

        @pipeline_app.command('monitor', rich_help_panel='Pipeline')
        def monitor_pipeline() -> None:
            """Tail every configured monitor source for this pipeline as one interleaved stream."""
            run_monitor(pipeline, config)

    return pipeline_app


class PipelineGroup(TyperGroup):
    """One pipeline's command group, whose tree is built the first time a command under it is asked for.

    Root help and shell completion read a pipeline's name, help and panel and nothing below them,
    so a run builds the tree of the one pipeline it names. Building every pipeline's tree on every
    run made each command's start grow with the number of pipeline files."""

    def __init__(self, name: str, pipeline: PipelineConfig, config: DectlConfig) -> None:
        super().__init__(
            name=name,
            help=f'[bold]{name}[/bold] pipeline — {pipeline_summary(pipeline, config)}',
            no_args_is_help=True,
            rich_help_panel='Pipelines',
        )
        self.pipeline_name = name
        self.pipeline = pipeline
        self.config = config
        self.built = False

    def build(self) -> None:
        if not self.built:
            tree = typer.main.get_group(make_pipeline_app(self.pipeline_name, self.pipeline, self.config))
            self.commands.update(tree.commands)
            self.built = True

    def list_commands(self, ctx: Any) -> list[str]:
        self.build()
        return super().list_commands(ctx)

    def get_command(self, ctx: Any, cmd_name: str) -> Any:
        self.build()
        return super().get_command(ctx, cmd_name)


# A pipeline configuring no resource has no commands, so it is never attached to the tree.
ATTACHED_PIPELINES: list[str] = [name for name, pipeline in cfg.pipelines.items() if pipeline_has_commands(pipeline, cfg)] if cfg else []


def root_examples(config: DectlConfig | None, attached: list[str]) -> str:
    """The root screen's examples, spelled with the config's own names so each runs as typed."""
    if config is None or not attached:
        return (
            'Examples:\n\ndectl config init — write a starting config that shows every option\n\ndectl reference — the full command grammar'
        )
    pipelines = {name: config.pipelines[name] for name in attached}
    lines = [f'dectl {attached[0]} list — the aliases this pipeline takes, and the AWS names behind them']
    job = next(((name, alias) for name, pipeline in pipelines.items() for alias in pipeline.glue_jobs), None)
    if job:
        lines.append(f'dectl {job[0]} glue {job[1]} deploy — upload the script, confirming any change to the job first')
        lines.append(f'dectl {job[0]} glue {job[1]} run --follow — start the job and tail it until it ends')
    function = next(
        ((name, alias) for name, pipeline in pipelines.items() for alias, fn in pipeline.lambdas.items() if fn.live_alias), None
    )
    if function:
        lines.append(f'dectl {function[0]} lambda {function[1]} deploy --publish — deploy, then move the live alias')
    monitored = next((name for name, pipeline in pipelines.items() if pipeline.monitor.selected_aliases()), None)
    if monitored:
        lines.append(f'dectl {monitored} monitor — tail every monitored resource as one stream')
    return 'Examples:\n\n' + '\n\n'.join(lines)


# The option-syntax brackets ([--follow], [OPTIONS], ...) would be parsed as rich style tags, so
# these lines are printed with markup disabled; section headers get their style via a style arg.
REFERENCE_INSTANCE_VERBS = [
    'glue    ALIAS  plan · deploy [--yes] · run [--follow] · logs [RUN_ID] [--follow] · runs [--limit N] [--json]',
    'lambda  ALIAS  deploy [--publish] · run [--payload-file F|-] [--json] · logs [--follow]',
    'sfn     ALIAS  run [--payload-file F|-] [--follow] · logs [ARN] [--follow] · runs [--limit N] [--json]',
    '               plan [--publish] · deploy [--publish]   (a machine configured with definition_file)',
    's3      ALIAS  mount · unmount · uri',
    'iceberg ALIAS  snapshots [--limit N] [--json] · history [--limit N] [--json]',
    '               files [SNAPSHOT] [--limit N] [--json] · branches [--limit N] [--json]',
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
    'jenkins ACTION [--follow] · jenkins status [--json] · jenkins logs [--follow]',
    "        (ACTION: a key of the job's actions in config)",
    'list [--json] · monitor',
]
REFERENCE_GLOBAL = [
    'reference · env [--json] · list [--json] · search KEYWORD [--json] · update [--check]',
    'config  init · show [--json] · path · example · edit · validate [--json]',
]


@app.command(
    rich_help_panel='Global commands',
    epilog='Example:\n\ndectl reference — every resource and verb, readable before any config exists',
)
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
        configured = [kind.word for kind in RESOURCE_KINDS if any(kind.attached(pipeline, cfg) for pipeline in cfg.pipelines.values())]
        info(f'Configured on this machine: {", ".join(configured) or "(none)"}')
    else:
        info('No config on this machine yet — run "dectl config init".')


def update_config() -> 'pyselfupdate.Config':
    """What `update` installs and what the root callback's daily check compares against.

    One source, so the notice cannot name a release the update command would not install. No
    token is configured because pyselfupdate authenticates itself, running `gh auth token` when a
    request is about to be made. `$GITHUB_TOKEN_COMMAND` redirects that or empties it, and it is
    the user's lever rather than ours.

    pyselfupdate is imported here rather than at the top, so `--help`, `--version` and shell
    completion, which never reach the callback, do not load it."""
    import pyselfupdate

    return pyselfupdate.Config(tool='dectl', owner='datapointchris')


def installed_commit() -> str | None:
    """The git commit dectl was installed from, when it was installed from a ref.

    uv records the resolved commit in `direct_url.json` for any git install, a pinned
    tag included, which is the install the README gives. A wheel from an index
    carries no such file and gets None, so one implementation covers both."""
    try:
        text = importlib.metadata.distribution('dectl').read_text('direct_url.json')
    except importlib.metadata.PackageNotFoundError:
        return None
    if not text:
        return None
    return json.loads(text).get('vcs_info', {}).get('commit_id')


def version_callback(asked: bool) -> None:
    """`dectl --version`, in the one line every CLI here answers it with.

    Eager, so it answers before the root callback resolves an environment or a
    config — a tool that cannot say what it is because its config is unreadable
    is the case this is most often reached for."""
    if not asked:
        return
    version = installed_version()
    commit = installed_commit()
    print(f'dectl {version}{f" @ {commit[:8]}" if commit else ""}')
    raise typer.Exit()


@app.command(
    rich_help_panel='Global commands',
    epilog=(
        'Examples:\n\n'
        'dectl update — install the latest release\n\n'
        'dectl update --check — say whether a newer release exists, installing nothing'
    ),
)
def update(
    check_only: Annotated[bool, typer.Option('--check', help='Report whether an update is available without installing it')] = False,
) -> None:
    """Update dectl to the latest published GitHub release."""
    from pyselfupdate.typercmd import run_update

    run_update(update_config(), check_only=check_only)


@app.callback(
    invoke_without_command=True,
    epilog=root_examples(cfg, ATTACHED_PIPELINES),
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
    version: Annotated[
        bool | None,
        typer.Option('--version', callback=version_callback, is_eager=True, help='Show the installed version and exit.'),
    ] = None,
) -> None:
    """[bold]dectl[/bold] — data engineering control for AWS pipelines.

    Commands follow the shape: [bold]dectl PIPELINE RESOURCE ALIAS VERB [OPTIONS][/bold] — the
    verb comes last, so a deploy → run → logs loop on one resource changes only the final word.

    [bold]One rule: aliased vs. set.[/bold] Acting on one thing takes an alias
    ([bold]glue JOB run[/bold], [bold]s3 BUCKET mount[/bold]); acting on the whole set takes none
    ([bold]s3 export[/bold], [bold]jenkins status[/bold], [bold]list[/bold], [bold]monitor[/bold]).

    Start with [bold]dectl PIPELINE list[/bold]. It maps each alias, the short config key the commands
    take, to the AWS name it acts on. Any partial command run bare shows what comes next, and
    [bold]dectl reference[/bold] prints the whole grammar.

    Tail one resource with its [bold]logs[/bold] verb. Reach for [bold]dectl PIPELINE monitor[/bold] when a
    run crosses resources, such as a state machine starting a Lambda: it merges them into one stream.
    [bold]dectl search[/bold] finds an AWS name your config does not hold.

    Config names carry an [bold]{env}[/bold] placeholder; [bold]--env prod[/bold] (or [bold]DECTL_ENV=prod[/bold])
    substitutes it, so one config drives every environment.
    """
    set_active_environment(env, resolve_env_source(ctx))
    set_no_input(no_input)
    # Never raises and never prints an error; the notice is deferred to exit so it lands after the
    # command's own output. `dectl update` is the only place an update failure is reported, and is
    # skipped here because it is about to do the thing the notice would suggest.
    if ctx.invoked_subcommand != 'update':
        from pyselfupdate import notify

        notify(update_config())
    if ctx.invoked_subcommand is None:
        print_environment_banner()
        if CONFIG_ERROR is not None:
            report_config_error(CONFIG_ERROR)
            stderr_console.print('pipeline commands are absent from the help below until it loads')
        print(ctx.get_help())


@app.command(
    rich_help_panel='Global commands',
    # Built from the scan's own table, so the help names exactly the services searched.
    help=(
        f'Search AWS by keyword across {", ".join(SEARCHERS)}.\n\n'
        'Scans each of those services in your default region and prints one table per service '
        'where a name contains the keyword. Useful for finding the real AWS name behind a config '
        'alias, or checking whether a resource exists.'
    ),
    epilog=(
        'Examples:\n\n'
        'dectl search sales — every AWS name containing "sales", one table per service\n\n'
        'dectl search sales --json — the same matches as JSON, for a script'
    ),
)
def search(
    keyword: Annotated[str, typer.Argument(help='Case-insensitive substring to match against resource names')],
    as_json: Annotated[bool, typer.Option('--json', help='Emit machine-readable JSON to stdout.')] = False,
) -> None:
    config = require_config()
    session = make_session(config)
    run_search(session, keyword, config.defaults.region, as_json=as_json)


@app.command(
    'env',
    rich_help_panel='Global commands',
    epilog=(
        'Examples:\n\n'
        'dectl env — the environment substituted for {env}, and where it was set\n\n'
        'dectl --env prod env — confirm --env prod takes effect before running against it\n\n'
        'dectl env --json — the environment with account_id, region and aws_profile'
    ),
)
def show_env(
    as_json: Annotated[bool, typer.Option('--json', help='Emit machine-readable JSON to stdout.')] = False,
) -> None:
    """Show the active environment and where it was resolved from.

    The environment is what every command substitutes for {env}. --json adds the
    config's other defaults as the file writes them: account_id, region and aws_profile. No
    other machine-readable output carries them, and `aws_profile` decides which AWS account a
    deploy reaches.
    """
    if not as_json:
        print_environment_banner()
        return

    # Every field of `Defaults` beside the environment, derived from the model so a field added
    # to it reaches this without an edit. The environment is the resolved one rather than the
    # file's, and its source is the fact only this document carries.
    config = require_config()
    resolved = {field: getattr(config.defaults, field) for field in Defaults.model_fields if field != 'environment'}
    emit_json({'environment': active_environment.name, 'source': active_environment.source, **resolved})


@app.command(
    'list',
    rich_help_panel='Global commands',
    epilog=(
        'Examples:\n\n'
        'dectl list — every pipeline, with each alias and the AWS name behind it\n\n'
        'dectl list --json — the same pipelines as JSON, for a script'
    ),
)
def list_all(
    as_json: Annotated[bool, typer.Option('--json', help='Emit machine-readable JSON to stdout.')] = False,
) -> None:
    """List every pipeline with its jobs, functions, and buckets (alias → AWS name)."""
    config = require_config()

    if as_json:
        emit_json(list(starmap(pipeline_to_dict, config.pipelines.items())))
        return
    for name, p in config.pipelines.items():
        render_pipeline(name, p)
