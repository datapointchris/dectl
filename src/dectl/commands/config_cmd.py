import typer

from dectl.config import CONFIG_PATH
from dectl.config import init_config
from dectl.config import load_config
from dectl.output import console
from dectl.output import error
from dectl.output import info
from dectl.output import success

config_app = typer.Typer(
    no_args_is_help=True,
    help='Manage dectl configuration at ~/.config/dectl/config.yaml.',
)


@config_app.command('init')
def config_init() -> None:
    """Create a starter config file (fails if one already exists)."""
    if CONFIG_PATH.exists():
        error(f'config already exists at {CONFIG_PATH}')
        raise typer.Exit(1)
    path = init_config()
    success(f'created config at {path}')


@config_app.command('show')
def config_show() -> None:
    """Display the loaded pipelines and their resources (alias → AWS name)."""
    cfg = load_config()
    if cfg is None:
        error(f'no config found at {CONFIG_PATH}')
        info('run "dectl config init" to create one')
        raise typer.Exit(1)

    console.print(f'[bold]config:[/bold] {CONFIG_PATH}')
    console.print()

    for name, pipeline in cfg.pipelines.items():
        types = []
        if pipeline.glue_jobs:
            types.append('glue')
        if pipeline.lambdas:
            types.append('lambda')
        if pipeline.step_functions:
            types.append('sfn')
        if pipeline.buckets:
            types.append('s3')
        console.print(f'[bold]{name}[/bold] ({", ".join(types)})')
        for alias, job in pipeline.glue_jobs.items():
            console.print(f'  glue/{alias}: {job.name}')
        for alias, fn in pipeline.lambdas.items():
            console.print(f'  lambda/{alias}: {fn.name}')
        for alias, sfn in pipeline.step_functions.items():
            console.print(f'  sfn/{alias}: {sfn.name}')
        for shortname, bucket in pipeline.buckets.items():
            console.print(f'  s3/{shortname}: {bucket}')
        console.print()
