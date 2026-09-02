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
from dectl.output import stderr_console


def expands_at_load(value: str) -> None:
    """Check `~` expansion, unless a `{env}` makes the real value unknowable here.

    A token can sit anywhere, `~{env}/code` included, so no stand-in preserves both the shape
    and the user name. A templated path is decided at the resolution door instead, where
    `expand_home_or_exit` reports it."""
    if '{env}' in value:
        return
    expand_home(value)


def expand_home(value: str) -> Path:
    """`~` expansion that reports an unresolvable home as a config error.

    `Path.expanduser()` raises RuntimeError for a `~user` naming nobody — `~code/x`, a missing
    slash, is the likeliest typo in a path key. Pydantic does not wrap RuntimeError into a
    ValidationError, and `CONFIG_LOAD_ERRORS` catches only YAMLError and ValidationError, so an
    uncaught one escapes main.py's import-time guard and takes down every command, including the
    `config` commands that exist to repair the file. Raising ValueError puts it back inside the
    schema failure every caller already handles."""
    try:
        return Path(value).expanduser()
    except RuntimeError as exc:
        raise ValueError(f'{value!r} names a home directory that cannot be resolved on this machine') from exc


def expand_home_or_exit(value: str) -> Path:
    """`expand_home`, with the failure reported to the reader instead of raised.

    The one door for a value whose `~` was not decidable at load. A `{env}` defers the check,
    and no model carrying such a value is re-validated on the read path — `render_env_model`
    runs per resource inside a verb, and `config validate`, `config show` and `list` never call
    it — so a bare ValueError from here reaches the reader as a traceback from the command whose
    job is to report the problem. Every configured path reaches disk through `pipeline_root` or
    `resolve_from_root` and both come here, so the root, a glue script and a lambda `source_dir`
    all fail the same way."""
    try:
        return expand_home(value)
    except ValueError as exc:
        error(f'a configured path cannot be resolved: {exc}')
        stderr_console.print('run "dectl config edit" to fix it')
        raise typer.Exit(1) from exc


class PathKind(StrEnum):
    """What a declared path has to be on disk.

    An enum rather than an `is_dir` boolean because every construction site passed it
    positionally beside a second boolean, where swapping the pair type-checks and both
    directions fail silently — a glue script checked as a directory reports the wrong fault on
    every deploy, and a lambda source checked as a key refuses the absolute path the config
    explicitly allows."""

    FILE = 'file'
    DIRECTORY = 'directory'


class PathFault(StrEnum):
    """Why a declared path cannot be used.

    A name callers branch on. The sentence a reader sees is in FAULT_WORDING, so rewording the
    message does not break `validate --json`, whose `fault` key is this value."""

    ABSENT = 'absent'
    EXPECTED_DIRECTORY = 'expected_directory'
    EXPECTED_FILE = 'expected_file'
    NOT_A_CLEAN_KEY = 'not_a_clean_key'
    ESCAPES_ROOT = 'escapes_root'


# The faults about how a value is spelled rather than about what is on disk. They are reported
# against the configured string, since resolution normalises away the very thing they name.
KEY_FAULTS = frozenset({PathFault.NOT_A_CLEAN_KEY, PathFault.ESCAPES_ROOT})

FAULT_WORDING = {
    PathFault.ABSENT: 'not found',
    PathFault.EXPECTED_DIRECTORY: 'is a file, not a directory',
    PathFault.EXPECTED_FILE: 'is a directory, not a file',
    PathFault.NOT_A_CLEAN_KEY: 'is not written as a plain relative path, and the S3 key is built from it',
    PathFault.ESCAPES_ROOT: 'climbs out of the directory paths resolve from, and the S3 key is built from it',
}


class PathSite(NamedTuple):
    """Where in a config one path-shaped value lives.

    The three components are separate fields and `label` is derived from them, in one place.
    Folding them into a sentence and recovering them with a split makes a string built for a
    human to read load-bearing for a lookup. Carried by both `DeclaredPath` and `UnusablePath`
    so the derivation is not written twice, one class apart."""

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

    Separate from `DeclaredPath` because the two questions have different subjects. A
    `script_prefix` shapes a key and names nothing on disk, so it cannot be a path at all — and
    while the two were one record it had nowhere to be checked."""

    site: PathSite
    value: str


class UnusablePath(NamedTuple):
    """One declared value that this config or this machine cannot supply, and why."""

    pipeline: str
    site: PathSite
    path: Path
    fault: PathFault
    # The string as written. A key fault is about the spelling, and `path` has already had the
    # `.`, the doubled separator and the trailing slash collapsed out of it by resolution — so
    # three differently-malformed entries render as one identical row without this.
    configured: str

    @property
    def shown(self) -> str:
        """The value to show the reader: what they wrote for a spelling fault, the resolved
        path for a fault about what is on disk."""
        return self.configured if self.fault in KEY_FAULTS else str(self.path)

    def __str__(self) -> str:
        return f'{self.pipeline}: {self.site.label} {FAULT_WORDING[self.fault]}: {self.shown}'


def path_fault(path: Path, expects: PathKind) -> PathFault | None:
    """Why this path on disk cannot be used, or None when it can.

    An absent path and one present with the wrong type have opposite remedies — check the tree
    out, or fix the config key that names it — so they never share a name."""
    if not path.exists():
        return PathFault.ABSENT
    if expects is PathKind.DIRECTORY and not path.is_dir():
        return PathFault.EXPECTED_DIRECTORY
    if expects is PathKind.FILE and not path.is_file():
        return PathFault.EXPECTED_FILE
    return None


def key_fault(configured: str) -> PathFault | None:
    """Why this configured string cannot become an S3 key, or None when it can.

    Tested against the string itself, never a `Path` round trip. `pathlib` collapses `.`, `//`
    and a trailing slash, which are exactly the shapes S3 stores literally — so a normalised
    probe agrees with the raw string on every input except the ones that matter.

    A leading `~` escapes the root the way an absolute path does. `PurePosixPath` calls it
    relative and holds no `..`, so both of the other arms miss it, and it resolves through
    `$HOME` — the same dependence on where the deploy ran that `resolve_paths_from` exists to
    remove, arriving by a different route.

    Checked here rather than in a field validator on purpose. A schema failure blanks the whole
    pipeline tree, because `main.py` falls back to `cfg = None` and every pipeline command
    disappears with it. A config carrying a malformed key still loads, `config validate` names
    it and the deploy refuses it, so the diagnosis reaches the reader without the CLI that
    delivers it going away."""
    if configured.startswith('~') or PurePosixPath(configured).is_absolute() or '..' in PurePosixPath(configured).parts:
        return PathFault.ESCAPES_ROOT
    if configured != str(PurePosixPath(configured)) or configured.endswith('/'):
        return PathFault.NOT_A_CLEAN_KEY
    return None


def refuse_unusable_paths(problems: list[UnusablePath]) -> None:
    """Print every unusable path and exit, or return when there are none.

    The one reporter for the deploy doors, so a glue deploy, a lambda deploy and `config
    validate` say the same sentence about the same fault. A door composing its own message
    instead diverges the moment the fault set grows, and the reader cannot tell whether two
    different sentences describe one problem."""
    if not problems:
        return
    for problem in problems:
        error(str(problem))
    error('run "dectl config show" to see where each configured path resolves')
    raise typer.Exit(1)
