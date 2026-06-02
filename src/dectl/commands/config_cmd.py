import typer

from dectl.config import CONFIG_PATH
from dectl.config import init_config
from dectl.config import load_config
from dectl.output import console
from dectl.output import error
from dectl.output import info
from dectl.output import success

config_app = typer.Typer(help='Manage dectl configuration')


@config_app.command('init')
def config_init() -> None:
    """Create a template config file."""
    if CONFIG_PATH.exists():
        error(f'config already exists at {CONFIG_PATH}')
        raise typer.Exit(1)
    path = init_config()
    success(f'created config at {path}')


@config_app.command('show')
def config_show() -> None:
    """Display the current config."""
    cfg = load_config()
    if cfg is None:
        error(f'no config found at {CONFIG_PATH}')
        info('run "dectl config init" to create one')
        raise typer.Exit(1)

    console.print(f'[dim]config: {CONFIG_PATH}[/dim]')
    console.print()

    for name, pipeline in cfg.pipelines.items():
        types = []
        if pipeline.glue_jobs:
            types.append('glue')
        if pipeline.lambdas:
            types.append('lambda')
        console.print(f'[bold]{name}[/bold] ({", ".join(types)})')
        for alias, job in pipeline.glue_jobs.items():
            console.print(f'  glue/{alias}: {job.name}')
        for alias, fn in pipeline.lambdas.items():
            console.print(f'  lambda/{alias}: {fn.name}')
        console.print()
