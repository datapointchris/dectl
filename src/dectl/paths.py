"""Whether a configured path is usable, and whether a configured string can become an S3 key.

Two questions, and they are not the same question. `path_fault` asks whether a resolved `Path`
is on this machine and is the right kind of thing. `key_fault` asks whether a string is spelled
in a way S3 will store as written. A glue script is subject to both — it is uploaded from disk
and named by a key built from the config string. A lambda `source_dir` is subject only to the
first, and a glue `script_prefix` only to the second, which is why `config.py` enumerates them
separately rather than flagging one record with a pair of booleans.

Nothing here knows what a pipeline is. The models and the walk over them are `config.py`'s;
what a fault is, how a key must be spelled and how a `~` expands are this module's.
"""

from enum import StrEnum
from pathlib import Path
from pathlib import PurePosixPath
from typing import NamedTuple

import typer

from dectl.output import error


def expand_home(value: str) -> Path:
    """`~` expansion, total: a `~user` naming nobody comes back as the literal string.

    `Path.expanduser()` raises RuntimeError for a `~user` that resolves to no home — `~code/x`,
    a missing slash, is the likeliest typo in a path key. `home_fault` is the reporting half."""
    try:
        return Path(value).expanduser()
    except RuntimeError:
        return Path(value)


class PathKind(StrEnum):
    """What a declared path has to be on disk."""

    FILE = 'file'
    DIRECTORY = 'directory'
    # A directory that also has to hold something. A lambda `source_dir` is zipped whole, and an
    # archive with no entries is 22 bytes, perfectly valid, and accepted by
    # `update_function_code` — so an empty one replaces the function's code with nothing.
    NON_EMPTY_DIRECTORY = 'non_empty_directory'


class PathFault(StrEnum):
    """Why a path-bearing config field cannot be used.

    A name callers branch on. The sentence a reader sees is in FAULT_WORDING, so rewording the
    message does not break `validate --json`, whose `fault` key is this value.

    The boundary is the field, not the value: `DECLARES_NOTHING` says a path-bearing field
    named no path at all, and the key faults say a string an S3 key is built from is spelled in
    a way S3 stores differently. A config string that is neither — a bucket name — is not one of
    these, and stretching the enum to cover it would put two unrelated questions in one record
    again."""

    ABSENT = 'absent'
    EXPECTED_DIRECTORY = 'expected_directory'
    EXPECTED_FILE = 'expected_file'
    EMPTY_DIRECTORY = 'empty_directory'
    DECLARES_NOTHING = 'declares_nothing'
    UNRESOLVABLE_HOME = 'unresolvable_home'
    NOT_A_CLEAN_KEY = 'not_a_clean_key'
    ESCAPES_ROOT = 'escapes_root'


# The faults about how a value is spelled rather than about what is on disk. They are reported
# against the configured string, since resolution normalises away the very thing they name.
KEY_FAULTS = frozenset({PathFault.NOT_A_CLEAN_KEY, PathFault.ESCAPES_ROOT})

# Every member gets a sentence, and a missing one raises at the moment a reader needs the
# answer. `test_every_fault_has_a_sentence` is what keeps the mapping total; `KEY_FAULTS` is
# the other property beside this enum and fails in the opposite direction, so it is pinned too.
FAULT_WORDING = {
    PathFault.ABSENT: 'not found',
    PathFault.EXPECTED_DIRECTORY: 'is a file, not a directory',
    PathFault.EXPECTED_FILE: 'is a directory, not a file',
    PathFault.EMPTY_DIRECTORY: 'holds nothing to deploy',
    PathFault.DECLARES_NOTHING: 'names nothing to deploy',
    PathFault.UNRESOLVABLE_HOME: 'names a home directory this machine cannot resolve',
    PathFault.NOT_A_CLEAN_KEY: 'is not written as a plain relative path, and the S3 key is built from it',
    PathFault.ESCAPES_ROOT: 'climbs out of the directory paths resolve from, and the S3 key is built from it',
}

# The form a key-shaped value has to take, said once. An error naming what is wrong without
# naming what is right leaves the reader to guess, and the two key faults are the ones where
# `config show` cannot help — it prints the resolved path, which has the malformation
# normalised out of it and looks correct.
KEY_FORM = 'a glue key is written as plain relative segments: jobs/copy.py, never ./x, ../x, ~/x, /x, a//b or a trailing /'


class PathSite(NamedTuple):
    """Where in a config one path-shaped value lives, and how it is named to a reader.

    Carried by `DeclaredPath` and `UnusablePath` alike, so `label` has one definition. An alias
    holding a space is what separates these three fields from a `label` a consumer splits."""

    resource: str
    # Empty for `resolve_paths_from`, which belongs to the pipeline rather than to a resource.
    alias: str
    # The config key, spelled the way the file spells it, so a reader filtering `validate
    # --json` on the key they wrote finds their own row.
    field: str

    @property
    def label(self) -> str:
        """How this value is named to a reader, in errors and in `config validate`."""
        return f'{self.resource}/{self.alias} {self.field}' if self.alias else self.field


class DeclaredPath(NamedTuple):
    """One filesystem path a pipeline's config names, with `{env}` already substituted."""

    site: PathSite
    # The pipeline-model collection holding this value's owner, so a dump can drop it by name
    # rather than by a branch on the resource. Empty for the pipeline's own key, dropped by
    # name. Carried rather than reconstructed: a resource nobody wrote a branch for otherwise
    # falls through and stays in the dump.
    collection: str
    value: str
    expects: PathKind


class DeclaredKey(NamedTuple):
    """One configured string an S3 key is built from, with `{env}` already substituted.

    A `script_prefix` shapes a key and names nothing on disk, so it is one of these and not a
    `DeclaredPath`. A glue script is both."""

    site: PathSite
    value: str


class UnusablePath(NamedTuple):
    """One declared value that this config or this machine cannot supply, and why."""

    pipeline: str
    site: PathSite
    # Where the value resolved to, or None when it resolves to nothing on this machine: a
    # `script_prefix` is an S3 key prefix, and a glue job declaring no scripts has no value at
    # all. `Path('')` is `.`, so a non-optional field publishes a path the config never named.
    path: Path | None
    fault: PathFault
    # The string as written. A key fault is about the spelling, and `path` has already had the
    # `.`, the doubled separator and the trailing slash collapsed out of it by resolution — so
    # three differently-malformed entries render as one identical row without this.
    configured: str

    @property
    def shown(self) -> str | None:
        """The value to show the reader, or None when the fault is that there is no value.

        What they wrote for a spelling fault, the resolved path otherwise. A spelling fault is
        quoted because the characters are the finding: bare, a trailing slash sits at the end of
        a sentence where nothing marks it, and an empty prefix shows as nothing at all."""
        if self.fault in KEY_FAULTS:
            return repr(self.configured)
        if self.path is not None:
            return str(self.path)
        return None

    def __str__(self) -> str:
        shown = self.shown
        line = f'{self.pipeline}: {self.site.label} {FAULT_WORDING[self.fault]}'
        return f'{line}: {shown}' if shown is not None else line


def home_fault(configured: str) -> PathFault | None:
    """UNRESOLVABLE_HOME when this string's `~user` names nobody on this machine.

    Asked at the same place `path_fault` is, so the diagnosis says which of the two happened.
    Left to `path_fault` alone, an unexpandable `~code/x` reports as `not found: ~code/x`,
    which sends the reader looking for a directory rather than at the missing slash."""
    try:
        Path(configured).expanduser()
    except RuntimeError:
        return PathFault.UNRESOLVABLE_HOME
    return None


DIRECTORY_KINDS = frozenset({PathKind.DIRECTORY, PathKind.NON_EMPTY_DIRECTORY})


def deployable_files(source: Path) -> list[Path]:
    """The files under `source` a deploy would send, bytecode caches excluded.

    One counter, read by `path_fault` and by `zip_lambda`, so `config validate` and the deploy
    cannot disagree about whether a directory holds anything to send."""
    return [found for found in sorted(source.rglob('*')) if found.is_file() and '__pycache__' not in found.parts]


def path_fault(path: Path, expects: PathKind) -> PathFault | None:
    """Why this path on disk cannot be used, or None when it can.

    An absent path and one present with the wrong type have opposite remedies — check the tree
    out, or fix the config key that names it — so they never share a name."""
    if not path.exists():
        return PathFault.ABSENT
    if expects in DIRECTORY_KINDS and not path.is_dir():
        return PathFault.EXPECTED_DIRECTORY
    if expects is PathKind.FILE and not path.is_file():
        return PathFault.EXPECTED_FILE
    if expects is PathKind.NON_EMPTY_DIRECTORY and not deployable_files(path):
        return PathFault.EMPTY_DIRECTORY
    return None


def key_fault(configured: str) -> PathFault | None:
    """Why this configured string cannot become an S3 key, or None when it can.

    Every segment has to be a plain name, tested as such. A round trip against `PurePosixPath`
    is the trap: it normalises `.`, `//` and a trailing slash away — the very shapes S3 stores
    literally — so it agrees with the raw string on every input that matters, and a bare `.` is
    its own normalised form.

    A leading `~` escapes the root the way an absolute path does. `PurePosixPath` calls it
    relative and holds no `..`, so both of the other arms miss it, and it resolves through
    `$HOME` — the same dependence on where the deploy ran that `resolve_paths_from` exists to
    remove, arriving by a different route.

    Checked here rather than in a field validator on purpose. A schema failure blanks the whole
    pipeline tree, because `main.py` falls back to `cfg = None` and every pipeline command
    disappears with it. A config carrying a malformed key still loads, `config validate` names
    it and the deploy refuses it, so the diagnosis reaches the reader without the CLI that
    delivers it going away."""
    if configured.startswith('~') or PurePosixPath(configured).is_absolute() or '..' in configured.split('/'):
        return PathFault.ESCAPES_ROOT
    if not all(segment and segment != '.' for segment in configured.split('/')):
        return PathFault.NOT_A_CLEAN_KEY
    return None


def recovery_lines(problems: list[UnusablePath]) -> list[str]:
    """What to tell the reader to do next, one line per kind of fault present.

    A spelling fault cannot be sent to `config show`: that command prints the *resolved* path,
    which has the `./`, the doubled separator and the trailing slash normalised out of it, so it
    renders a malformed key as a correct-looking one. Naming the form the key has to take is the
    answer to that failure; `config show` is the answer to a file that is not where the config
    says. A run carrying both gets both, because dropping either sends half the rows nowhere."""
    kinds = {problem.fault in KEY_FAULTS for problem in problems}
    lines = []
    if True in kinds:
        lines.append(KEY_FORM)
    if False in kinds:
        lines.append('run "dectl config show" to see where each configured path resolves')
    return lines


def refuse_unusable_paths(problems: list[UnusablePath]) -> None:
    """Print every unusable path and exit, or return when there are none.

    The one reporter, and the one place in the path domain that exits — so a glue deploy, a
    lambda deploy and `config validate` say the same sentence about the same fault, and nothing
    beneath a command that has committed to emitting a document can cut it short."""
    if not problems:
        return
    for problem in problems:
        error(str(problem))
    for line in recovery_lines(problems):
        error(line)
    raise typer.Exit(1)
