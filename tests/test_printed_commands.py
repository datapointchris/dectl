"""Every `dectl ...` command the source prints for a person resolves in the command tree."""

import io
import re
import tokenize
from collections.abc import Iterator
from pathlib import Path

import typer

from dectl.config import TEMPLATE_PIPELINES
from dectl.main import app

SOURCE = Path(__file__).parent.parent / 'src' / 'dectl'

# A command printed for a person: in rich bold, a code span or quotes, inside a string the
# program holds rather than a comment. A word running into a `/` or a `.` is a path argument,
# and an uppercase word such as PIPELINE is a placeholder, so the walk reads only the
# lowercase words before either.
PRINTED = re.compile(r"(?:\[bold\]|`|'|\")dectl((?: [a-z][\w-]*(?![\w/.-]))+)")

# The title `reference` prints over the grammar opens on the tool's name and names no command.
NOT_COMMANDS = {('command', 'grammar')}


def printed_commands() -> Iterator[tuple[str, list[str]]]:
    for source in sorted(SOURCE.rglob('*.py')):
        for token in tokenize.generate_tokens(io.StringIO(source.read_text()).readline):
            if token.type in {tokenize.STRING, tokenize.FSTRING_MIDDLE}:
                for match in PRINTED.finditer(token.string):
                    yield f'{source.relative_to(SOURCE)}:{token.start[0]}', match.group(1).split()


def test_every_command_it_prints_is_in_the_command_tree():
    """A hint naming a command that was renamed sends its reader to an error.

    A hint opening on a template pipeline's name is left out, because the root attaches a
    pipeline's commands only once a command names it. The walk stops at a leaf, so an argument
    after the command, as in `dectl search sales`, is not read as a subcommand."""
    tree = typer.main.get_command(app)
    missing = []
    walked = 0
    for site, words in printed_commands():
        if words[0] in TEMPLATE_PIPELINES or tuple(words) in NOT_COMMANDS:
            continue
        walked += 1
        command = tree
        for word in words:
            subcommands = getattr(command, 'commands', None)
            if subcommands is None:
                break
            if word not in subcommands:
                missing.append(f'{site}: dectl {" ".join(words)}')
                break
            command = subcommands[word]

    assert walked > 20
    assert missing == []
