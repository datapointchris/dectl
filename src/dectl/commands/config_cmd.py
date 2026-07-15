import os
import shlex
import shutil
import subprocess

import typer
import yaml
from pydantic import ValidationError
from rich.syntax import Syntax

from dectl.config import CONFIG_PATH
from dectl.config import TEMPLATE_CONFIG
from dectl.config import init_config
from dectl.config import load_config
from dectl.env import substitute_env
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
            console.print(f'  glue/{alias}: {substitute_env(job.name)}')
        for alias, fn in pipeline.lambdas.items():
            console.print(f'  lambda/{alias}: {substitute_env(fn.name)}')
        for alias, sfn in pipeline.step_functions.items():
            console.print(f'  sfn/{alias}: {substitute_env(sfn.name)}')
        for shortname, bucket in pipeline.buckets.items():
            console.print(f'  s3/{shortname}: {substitute_env(bucket)}')
        console.print()


@config_app.command('path')
def config_path() -> None:
    """Print the config file path (whether or not it exists)."""
    # Bare print (no rich markup/ANSI) so the output stays clean for shell substitution,
    # e.g. cd "$(dirname "$(dectl config path)")".
    print(CONFIG_PATH)


@config_app.command('example')
def config_example() -> None:
    """Print a full example config showing every available option.

    Meant for side-by-side reference: display it in one pane while editing the real
    config in another. Syntax-highlighted when printed to a terminal, plain text when
    redirected or piped so `dectl config example > config.yaml` stays clean.
    """
    if console.is_terminal:
        console.print(Syntax(TEMPLATE_CONFIG, 'yaml', background_color='default', word_wrap=True))
    else:
        print(TEMPLATE_CONFIG, end='')


@config_app.command('edit')
def config_edit() -> None:
    """Open the config in your editor ($VISUAL, then $EDITOR).

    Seeds a starter config from the template first if none exists. The editor runs in the
    foreground, so a terminal editor (nvim, vim) blocks until you close it; a GUI editor
    returns immediately unless you configured it to wait (e.g. EDITOR="code --wait").
    """
    if not CONFIG_PATH.exists():
        path = init_config()
        info(f'no config found — created a starter config at {path}')

    editor = os.environ.get('VISUAL') or os.environ.get('EDITOR')
    if not editor:
        error('no editor configured — set $VISUAL or $EDITOR')
        raise typer.Exit(1)

    # $EDITOR may carry arguments (e.g. "code --wait", "emacsclient -nw"), so split it and
    # resolve the binary to a full path — this keeps the user's full intent (including any
    # --wait) and satisfies bandit's partial-path check (B607) without a nosec.
    parts = shlex.split(editor)
    editor_bin = shutil.which(parts[0])
    if editor_bin is None:
        error(f'editor not found on PATH: {parts[0]}')
        raise typer.Exit(1)

    subprocess.run([editor_bin, *parts[1:], str(CONFIG_PATH)])


@config_app.command('validate')
def config_validate() -> None:
    """Check that the config exists, is valid YAML, and matches the expected schema."""
    if not CONFIG_PATH.exists():
        error(f'no config found at {CONFIG_PATH}')
        info('run "dectl config init" to create one')
        raise typer.Exit(1)

    try:
        load_config()
    except yaml.YAMLError as exc:
        error(f'config at {CONFIG_PATH} is not valid YAML:')
        console.print(f'  {exc}')
        raise typer.Exit(1) from exc
    except ValidationError as exc:
        error(f'config at {CONFIG_PATH} does not match the expected schema:')
        for err in exc.errors():
            location = '.'.join(str(part) for part in err['loc']) or '(root)'
            console.print(f'  {location}: {err["msg"]}')
        raise typer.Exit(1) from exc

    success(f'config at {CONFIG_PATH} is valid')
