import os
import shlex
import shutil
import subprocess
from itertools import starmap
from typing import Annotated
from typing import NoReturn

import typer
from rich.syntax import Syntax

from dectl.config import CONFIG_LOAD_ERRORS
from dectl.config import CONFIG_PATH
from dectl.config import TEMPLATE_CONFIG
from dectl.config import Defaults
from dectl.config import config_error_outcome
from dectl.config import config_value_faults
from dectl.config import init_config
from dectl.config import load_config
from dectl.config import report_config_error
from dectl.output import console
from dectl.output import emit_json
from dectl.output import error
from dectl.output import info
from dectl.output import success
from dectl.pipeline_view import pipeline_to_dict
from dectl.pipeline_view import render_pipeline
from dectl.values import refuse_unusable_values

config_app = typer.Typer(
    no_args_is_help=True,
    help=f'Manage dectl configuration at {CONFIG_PATH}.',
)

# What `config validate` exits for anything it found. The convention it follows reserves 1 for
# drift a tool's own `apply` clears without a person, and dectl ships no `apply` — every fault it
# reports is a config a person has to edit, whether the file was unreadable or merely names a
# directory that is not there. A scheduler holding that convention would read 1 as self-clearing
# and let a broken deploy config sit. 2 is not available, because Typer spends it on usage.
#
# `--json`'s `outcome` carries the finer split, which is where a caller that wants it should look:
# `no_config`, `invalid_yaml`, `invalid_schema` and `unusable_values` are four answers, and an
# exit code that tried to carry them would be inventing a vocabulary this one already has.
NEEDS_A_PERSON_EXIT = 3


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
    # Derived from the model rather than listed, so a field added to `Defaults` prints without
    # this being edited. `aws_profile` is the one that decides which AWS account every deploy
    # below reaches, and a reader could not see it resolved anywhere.
    for field in Defaults.model_fields:
        console.print(f'  {field}: {getattr(cfg.defaults, field) or "(unset)"}')
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
def config_validate(
    as_json: Annotated[bool, typer.Option('--json', help='Emit the unusable values as machine-readable JSON to stdout.')] = False,
) -> None:
    """Check the config parses, matches the schema, and that what it names can be used.

    A pipeline that names `resolve_paths_from` is checked all the way down: the directory
    itself, every glue script, and every lambda source_dir. A pipeline that names none is still
    checked for how its S3 keys are spelled and whether its bucket names are ones S3 accepts,
    both answerable on any machine; its files are not, since they resolve from wherever you run
    dectl.

    --json emits an object carrying `outcome` and `unusable_values`. The outcome separates a
    clean config from one that could not be read at all, which an empty list alone folds
    together — the detail of a load failure stays on stderr, where it has a renderer.

    0 is a clean config and 3 is everything else, because every fault this reports needs a
    person to edit the file — the convention reserves 1 for drift a tool's own `apply` clears,
    and dectl has no `apply`. Which kind of fault it was is `outcome`, not the exit code.
    """

    def refuse(outcome: str, message: str) -> NoReturn:
        """Report on stderr, keep stdout parseable, exit non-zero.

        The document carries the outcome beside the list. A bare `[]` reads the same whether the
        config was clean, unreadable, or absent — so a caller that has dropped the exit status is
        told nothing was wrong with a config that was never read. Every caller of this is a
        config that could not be read, which is the code a person has to act on.

        `NoReturn`, because every path through it raises: `-> None` reads as fall-through and
        invites a call site to guard against a return that cannot happen."""
        error(message)
        if as_json:
            emit_json({'outcome': outcome, 'unusable_values': []})
        raise typer.Exit(NEEDS_A_PERSON_EXIT)

    if not CONFIG_PATH.exists():
        refuse('no_config', f'no config found at {CONFIG_PATH} — run "dectl config init" to create one')

    try:
        config = load_config()
    except CONFIG_LOAD_ERRORS as exc:
        report_config_error(exc)
        if as_json:
            emit_json({'outcome': config_error_outcome(exc), 'unusable_values': []})
        raise typer.Exit(NEEDS_A_PERSON_EXIT) from exc
    if config is None:
        refuse('no_config', f'config at {CONFIG_PATH} was removed while it was being read')

    problems = config_value_faults(config)
    if as_json:
        emit_json(
            {
                'outcome': 'unusable_values' if problems else 'valid',
                'unusable_values': [
                    {
                        'pipeline': p.pipeline,
                        'resource': p.site.resource,
                        'alias': p.site.alias,
                        'field': p.site.field,
                        'configured': p.configured,
                        'path': str(p.path) if p.path is not None else None,
                        'fault': str(p.fault),
                    }
                    for p in problems
                ],
            }
        )
        raise typer.Exit(NEEDS_A_PERSON_EXIT if problems else 0)

    if problems:
        error(f'config at {CONFIG_PATH} matches the schema, but names values that cannot be used:')
        refuse_unusable_values(problems, exit_code=NEEDS_A_PERSON_EXIT)

    success(f'config at {CONFIG_PATH} is valid')
