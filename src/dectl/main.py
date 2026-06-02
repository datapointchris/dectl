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

app = typer.Typer(name='dectl', invoke_without_command=True)
app.add_typer(config_app, name='config')


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
if cfg:
    for name, pipeline in cfg.pipelines.items():
        pipeline_app = typer.Typer(help=f'Pipeline: {name}')
        has_commands = False
        if pipeline.glue_jobs:
            pipeline_app.add_typer(make_glue_app(name, pipeline, cfg), name='glue')
            has_commands = True
        if pipeline.lambdas:
            pipeline_app.add_typer(make_lambda_app(name, pipeline, cfg), name='lambda')
            has_commands = True
        if pipeline.jenkins and cfg.jenkins:
            pipeline_app.add_typer(make_deploy_app(name, pipeline.jenkins, cfg), name='deploy')
            has_commands = True
        if has_commands:

            def _make_list_cmd(papp: typer.Typer, pname: str, pconfig: PipelineConfig):
                @papp.command('list')
                def list_resources() -> None:
                    """List resources for this pipeline."""
                    print_pipeline(pname, pconfig)

            _make_list_cmd(pipeline_app, name, pipeline)
            app.add_typer(pipeline_app, name=name)


@app.command()
def update() -> None:
    """Reinstall dectl from source."""
    import subprocess

    source_dir = Path.home() / 'code' / 'dectl'
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


@app.callback(invoke_without_command=True)
def main(ctx: typer.Context) -> None:
    """dectl - data engineering control."""
    if ctx.invoked_subcommand is None:
        print(ctx.get_help())


@app.command()
def search(
    keyword: Annotated[str, typer.Argument(help='Keyword to search across AWS resources')],
) -> None:
    """Search AWS resources by keyword across services."""
    if not cfg:
        error('no config loaded')
        raise typer.Exit(1)
    session = make_session(cfg)
    run_search(session, keyword, cfg.defaults.region)


@app.command('list')
def list_all() -> None:
    """List all pipelines and their resources."""
    if not cfg:
        error('no config loaded')
        raise typer.Exit(1)

    for name, p in cfg.pipelines.items():
        print_pipeline(name, p)
