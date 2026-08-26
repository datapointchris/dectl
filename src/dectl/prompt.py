import sys

import typer

from dectl.output import stderr_console


class Interactivity:
    """Whether this invocation may ask the user anything.

    Per-invocation state set from the root callback, like ActiveEnvironment in
    env.py, so a command deep in the tree can ask without --no-input being
    threaded through every signature between here and there."""

    def __init__(self) -> None:
        self.no_input = False


interactivity = Interactivity()


def set_no_input(no_input: bool) -> None:
    interactivity.no_input = no_input


def can_prompt() -> bool:
    """Whether a question may be asked: --no-input never allows one, and otherwise
    stdin has to be a terminal."""
    return not interactivity.no_input and sys.stdin.isatty()


def confirm_or_exit(question: str, flag: str = '--yes') -> None:
    """Ask for confirmation, or fail naming the flag that would have answered.

    A prompt is only ever offered on an interactive stdin. Prompting a
    non-interactive caller — a Jenkins step, a cron job, anything shelling out —
    blocks on a stdin that never closes, leaving it with no output and no exit
    code, which is the one failure it cannot recover from."""
    if not can_prompt():
        stderr_console.print(f'[red]Refusing to prompt without an interactive terminal; pass [bold]{flag}[/bold][/red]')
        raise typer.Exit(1)
    if not typer.confirm(question):
        raise typer.Abort()
