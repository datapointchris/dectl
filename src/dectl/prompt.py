import sys
from collections.abc import Callable

import typer

from dectl.output import stderr_console


def stdin_is_a_terminal() -> bool:
    return sys.stdin.isatty()


class Interactivity:
    """Whether this invocation may ask the user anything.

    Per-invocation state set from the root callback, like ActiveEnvironment in
    env.py, so a command deep in the tree can ask without --no-input being
    threaded through every signature between here and there.

    `stdin_is_terminal` is the terminal check, held here so a test sets it
    rather than patching `sys.stdin.isatty`, which pytest's capture can swap
    back between fixture setup and the test."""

    def __init__(self) -> None:
        self.no_input = False
        self.stdin_is_terminal: Callable[[], bool] = stdin_is_a_terminal


interactivity = Interactivity()


def set_no_input(no_input: bool) -> None:
    interactivity.no_input = no_input


def can_prompt() -> bool:
    """Whether a question may be asked: --no-input never allows one, and otherwise
    stdin has to be a terminal."""
    return not interactivity.no_input and interactivity.stdin_is_terminal()


def confirm_or_exit(question: str, flag: str = '--yes') -> None:
    """Ask for confirmation, or fail naming the flag that would have answered.

    A prompt is only ever offered on an interactive stdin. Prompting a
    non-interactive caller — a Jenkins step, a cron job, anything shelling out —
    blocks on a stdin that never closes, leaving it with no output and no exit
    code, which is the one failure it cannot recover from.

    The refusal exits 2, the usage-error code: the same command succeeds once
    the caller adds the flag."""
    if not can_prompt():
        stderr_console.print(f'[red]Refusing to prompt without an interactive terminal; pass [bold]{flag}[/bold][/red]')
        raise typer.Exit(2)
    if not typer.confirm(question):
        raise typer.Abort()
