import json
from typing import Any

from rich.console import Console

console = Console()
# Everything that is not data goes here: warnings, and every message a command refuses with.
# stdout belongs to the answer, so a `--json` read piped into jq and an `s3 export` inside
# eval "$(...)" both stay parseable on the path where the command fails.
stderr_console = Console(stderr=True)


def emit_json(data: Any) -> None:
    """Print data as JSON to stdout with a stable, machine-readable shape.

    Uses a bare print() rather than the rich console so no markup or ANSI escapes leak into
    output a script may pipe into jq. default=str renders datetimes and other non-JSON types
    as strings instead of raising."""
    print(json.dumps(data, indent=2, default=str))


def error(message: str) -> None:
    stderr_console.print(f'[red]{message}[/red]')


def success(message: str) -> None:
    console.print(f'[green]{message}[/green]')


def info(message: str) -> None:
    console.print(message)


def warn(message: str) -> None:
    stderr_console.print(f'[yellow]warning:[/yellow] {message}')


def format_duration(start, end) -> str:
    """Elapsed wall-clock between two datetimes, in the largest unit that stays readable.

    One spelling for every resource that shows a duration. A durable execution's elapsed time
    includes every suspended wait and an Iceberg diff can span a single commit or a month, so
    the range runs from sub-second to weeks and no single unit covers it.

    Seconds are carried below a minute. Two commits a few seconds apart are the common case for
    a streaming table, and a formatter that floors to whole minutes reports them as no time at
    all rather than as a small number."""
    if start is None or end is None:
        return ''
    seconds = abs((end - start).total_seconds())
    if seconds < 60:
        return f'{seconds:.1f}s'
    whole = int(seconds)
    minutes, whole_seconds = divmod(whole, 60)
    hours, minutes = divmod(minutes, 60)
    days, hours = divmod(hours, 24)
    if days:
        return f'{days}d{hours:02d}h'
    return f'{hours}h{minutes:02d}m' if hours else f'{minutes}m{whole_seconds:02d}s'
