from typing import Annotated

import typer

from dectl.commands.config_cmd import config_app
from dectl.commands.glue import make_glue_app
from dectl.commands.lambda_ import make_lambda_app
from dectl.commands.search import run_search
from dectl.config import load_config
from dectl.output import error
from dectl.output import info
from dectl.session import make_session

app = typer.Typer(name='dectl', invoke_without_command=True)
app.add_typer(config_app, name='config')

cfg = load_config()
if cfg:
    for name, pipeline in cfg.pipelines.items():
        if pipeline.type == 'glue' and pipeline.glue_jobs:
            app.add_typer(make_glue_app(name, pipeline, cfg), name=name)
        elif pipeline.type == 'lambda' and pipeline.lambdas:
            app.add_typer(make_lambda_app(name, pipeline, cfg), name=name)


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
def list_pipeline(
    pipeline: Annotated[str | None, typer.Argument(help='Pipeline name to show resources for')] = None,
) -> None:
    """List known resources for a pipeline."""
    if not cfg:
        error('no config loaded')
        raise typer.Exit(1)

    pipelines = cfg.pipelines
    if pipeline:
        if pipeline not in pipelines:
            known = ', '.join(pipelines.keys())
            error(f'unknown pipeline "{pipeline}". known: {known}')
            raise typer.Exit(1)
        pipelines = {pipeline: pipelines[pipeline]}

    for name, p in pipelines.items():
        info(f'[bold]{name}[/bold] ({p.type})')
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
        info('')
