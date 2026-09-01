import os
import shlex
import shutil
import subprocess
from itertools import starmap
from typing import Annotated

import typer
from rich.syntax import Syntax

from dectl.config import CONFIG_LOAD_ERRORS
from dectl.config import CONFIG_PATH
from dectl.config import TEMPLATE_CONFIG
from dectl.config import init_config
from dectl.config import load_config
from dectl.config import missing_declared_paths
from dectl.config import report_config_error
from dectl.output import console
from dectl.output import emit_json
from dectl.output import error
from dectl.output import info
from dectl.output import stderr_console
from dectl.output import success
from dectl.pipeline_view import pipeline_to_dict
from dectl.pipeline_view import render_pipeline

config_app = typer.Typer(
    no_args_is_help=True,
    help=f'Manage dectl configuration at {CONFIG_PATH}.',
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
def config_show(
    as_json: Annotated[bool, typer.Option('--json', help='Emit machine-readable JSON to stdout.')] = False,
) -> None:
    """Display the loaded pipelines and their resources (alias → AWS name)."""
    try:
        cfg = load_config()
    except CONFIG_LOAD_ERRORS as exc:
        report_config_error(exc)
        raise typer.Exit(1) from exc
    if cfg is None:
        error(f'no config found at {CONFIG_PATH} — run "dectl config init" to create one')
        raise typer.Exit(1)

    if as_json:
        emit_json(list(starmap(pipeline_to_dict, cfg.pipelines.items())))
        return

    console.print(f'[bold]config:[/bold] {CONFIG_PATH}')
    console.print()
    for name, pipeline in cfg.pipelines.items():
        render_pipeline(name, pipeline)


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
    """Check the config parses, matches the schema, and that declared repo paths are on this machine.

    A pipeline that names a `repo` is checked all the way down: the directory itself, every glue
    script, and every lambda source_dir. A pipeline that names none is left alone, since its
    paths resolve against wherever you run dectl from.
    """
    if not CONFIG_PATH.exists():
        error(f'no config found at {CONFIG_PATH} — run "dectl config init" to create one')
        raise typer.Exit(1)

    try:
        config = load_config()
    except CONFIG_LOAD_ERRORS as exc:
        report_config_error(exc)
        raise typer.Exit(1) from exc
    if config is None:
        error(f'config at {CONFIG_PATH} was removed while it was being read')
        raise typer.Exit(1)

    problems = missing_declared_paths(config)
    if problems:
        error(f'config at {CONFIG_PATH} matches the schema, but names paths this machine cannot supply:')
        for problem in problems:
            stderr_console.print(f'  {problem}', markup=False)
        raise typer.Exit(1)

    success(f'config at {CONFIG_PATH} is valid')
