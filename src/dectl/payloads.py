import sys
from pathlib import Path

import typer

from dectl.output import error


def read_payload(payload_file: str | None) -> str:
    """Resolve a run/execution payload to a JSON string.

    `--payload-file PATH` reads the file; `--payload-file -` reads stdin (so a payload can be
    piped in without shell-quoting a large JSON blob); nothing given defaults to an empty
    object. This is the documented default for feeding events to `lambda run` / `sfn run`."""
    if not payload_file:
        return '{}'
    if payload_file == '-':
        return sys.stdin.read()
    path = Path(payload_file)
    if not path.exists():
        error(f'payload file not found: {payload_file}')
        raise typer.Exit(1)
    return path.read_text()
