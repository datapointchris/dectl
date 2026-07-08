from pathlib import Path
from typing import Annotated

import typer

from dectl.commands.config_cmd import config_app
from dectl.commands.deploy import make_deploy_app
from dectl.commands.glue import make_glue_app
from dectl.commands.lambda_ import make_lambda_app
from dectl.commands.search import run_search
from dectl.config import PipelineConfig
from dectl.config import load_config
from dectl.output import error
from dectl.output import info
from dectl.output import success
from dectl.session import make_session

app = typer.Typer(name='dectl', invoke_without_command=True, no_args_is_help=True)
app.add_typer(config_app, name='config', rich_help_panel='Global commands')


def print_pipeline(name: str, p: PipelineConfig) -> None:
    types = []
    if p.glue_jobs:
        types.append('glue')
    if p.lambdas:
        types.append('lambda')
    info(f'[bold]{name}[/bold] ({", ".join(types)})')
    for alias, job in p.glue_jobs.items():
        info(f'  glue/{alias}: {job.name}')
        info(f'    bucket: s3://{job.script_bucket}/{job.script_prefix}/')
        for s in job.scripts:
            info(f'      {s}')
    for alias, fn in p.lambdas.items():
        info(f'  lambda/{alias}: {fn.name}')
    if p.buckets.raw:
        info(f'  s3/raw: {p.buckets.raw}')
    if p.buckets.curated:
        info(f'  s3/curated: {p.buckets.curated}')
    if p.buckets.error:
        info(f'  s3/error: {p.buckets.error}')
    info('')


cfg = load_config()
EXAMPLE_PIPELINE = next(iter(cfg.pipelines), 'my-pipeline') if cfg and cfg.pipelines else 'my-pipeline'
if cfg:
    for name, pipeline in cfg.pipelines.items():
        resources = []
        if pipeline.glue_jobs:
            resources.append(f'glue ({", ".join(pipeline.glue_jobs)})')
        if pipeline.lambdas:
            resources.append(f'lambda ({", ".join(pipeline.lambdas)})')
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
        if pipeline.jenkins and cfg.jenkins:
            pipeline_app.add_typer(make_deploy_app(name, pipeline.jenkins, cfg), name='deploy', rich_help_panel='Resources')
            has_commands = True
        if has_commands:

            def _make_list_cmd(papp: typer.Typer, pname: str, pconfig: PipelineConfig):
                @papp.command('list', rich_help_panel='Info')
                def list_resources() -> None:
                    """Show this pipeline's jobs, functions, and buckets with their real AWS names."""
                    print_pipeline(pname, pconfig)

            _make_list_cmd(pipeline_app, name, pipeline)
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
def main(ctx: typer.Context) -> None:
    """[bold]dectl[/bold] — data engineering control for AWS pipelines.

    Commands follow the shape: [bold]dectl PIPELINE RESOURCE ACTION [ALIAS] [OPTIONS][/bold]

    Pipelines come from your config (below, under Pipelines). Each pipeline
    exposes the resources it defines — [bold]glue[/bold], [bold]lambda[/bold], and/or [bold]deploy[/bold] (Jenkins).
    Every level is self-documenting: run any partial command with no arguments
    or [bold]--help[/bold] to see what is available next.

    Aliases (the JOB / FUNCTION names) are the short keys from your config, not
    the full AWS resource names — run [bold]dectl PIPELINE list[/bold] to see the mapping.
    """
    if ctx.invoked_subcommand is None:
        print(ctx.get_help())


@app.command(rich_help_panel='Global commands')
def search(
    keyword: Annotated[str, typer.Argument(help='Case-insensitive substring to match against resource names')],
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
    run_search(session, keyword, cfg.defaults.region)


@app.command('list', rich_help_panel='Global commands')
def list_all() -> None:
    """List every pipeline with its jobs, functions, and buckets (alias → AWS name)."""
    if not cfg:
        error('no config loaded — run "dectl config init" first')
        raise typer.Exit(1)

    for name, p in cfg.pipelines.items():
        print_pipeline(name, p)
