import json
from typing import Any

from rich.console import Console

console = Console()
# Warnings go to stderr so they never land in output a caller is consuming: `--json` piped into
# jq, or `s3 export` inside eval "$(...)".
warning_console = Console(stderr=True)


def emit_json(data: Any) -> None:
    """Print data as JSON to stdout with a stable, machine-readable shape.

    Uses a bare print() rather than the rich console so no markup or ANSI escapes leak into
    output a script may pipe into jq. default=str renders datetimes and other non-JSON types
    as strings instead of raising."""
    print(json.dumps(data, indent=2, default=str))


def error(message: str) -> None:
    console.print(f'[red]{message}[/red]')


def success(message: str) -> None:
    console.print(f'[green]{message}[/green]')


def info(message: str) -> None:
    console.print(message)


def warn(message: str) -> None:
    warning_console.print(f'[yellow]warning:[/yellow] {message}')
