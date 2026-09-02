"""Whether a value the config names outside the program can be what it has to be.

Three questions, and they are not the same question. `path_fault` asks whether a resolved
`Path` is on this machine and is the right kind of thing. `key_fault` asks whether a string is
spelled in a way S3 will store as written. `bucket_fault` asks whether a string can name an S3
bucket at all. A glue script answers the first two — it is uploaded from disk and named by a
key built from the config string. A lambda `source_dir` answers only the first, a glue
`script_prefix` only the second, and a bucket name only the third, which is why `config.py`
enumerates them as three records rather than flagging one with a set of booleans.

One vocabulary reports all three. `ConfigFault` is the name a caller branches on and
`validate --json` publishes; the records that feed it stay separate, because the record is
where conflating two questions leaves a value with nowhere to be declared.

Nothing here knows what a pipeline is. The models and the walk over them are `config.py`'s.
"""

import re
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


class ConfigFault(StrEnum):
    """Why a value the config names outside the program cannot be used.

    A name callers branch on. The sentence a reader sees is in FAULT_WORDING, so rewording the
    message does not break `validate --json`, whose `fault` key is this value."""

    ABSENT = 'absent'
    EXPECTED_DIRECTORY = 'expected_directory'
    EXPECTED_FILE = 'expected_file'
    EMPTY_DIRECTORY = 'empty_directory'
    DECLARES_NOTHING = 'declares_nothing'
    UNRESOLVABLE_HOME = 'unresolvable_home'
    NOT_A_CLEAN_KEY = 'not_a_clean_key'
    ESCAPES_ROOT = 'escapes_root'
    NOT_A_BUCKET_NAME = 'not_a_bucket_name'


# The faults about how a value is spelled rather than about what this machine holds. They are
# reported against the configured string: resolution normalises away the very thing a key fault
# names, and a bucket name resolves to nothing at all.
SPELLING_FAULTS = frozenset({ConfigFault.NOT_A_CLEAN_KEY, ConfigFault.ESCAPES_ROOT, ConfigFault.NOT_A_BUCKET_NAME})

# Every member gets a sentence, and a missing one raises at the moment a reader needs the
# answer. `test_every_fault_has_a_sentence` is what keeps the mapping total; `SPELLING_FAULTS`
# is the other property beside this enum and fails in the opposite direction, so it is pinned
# too — a member missing from it silently reports the resolved path for a fault about spelling.
FAULT_WORDING = {
    ConfigFault.ABSENT: 'not found',
    ConfigFault.EXPECTED_DIRECTORY: 'is a file, not a directory',
    ConfigFault.EXPECTED_FILE: 'is a directory, not a file',
    ConfigFault.EMPTY_DIRECTORY: 'holds nothing to deploy',
    ConfigFault.DECLARES_NOTHING: 'names nothing to deploy',
    ConfigFault.UNRESOLVABLE_HOME: 'names a home directory this machine cannot resolve',
    ConfigFault.NOT_A_CLEAN_KEY: 'is not written as a plain relative path, and the S3 key is built from it',
    ConfigFault.ESCAPES_ROOT: 'climbs out of the directory paths resolve from, and the S3 key is built from it',
    ConfigFault.NOT_A_BUCKET_NAME: 'is not a name S3 will accept for a bucket',
}

# The form each spelling-checked value has to take, said once per kind. An error naming what is
# wrong without naming what is right leaves the reader to guess, and these are the faults where
# `config show` cannot help: it prints the resolved path, which has the malformation normalised
# out of it, and it shows a bucket name back exactly as written.
KEY_FORM = 'a glue key is written as plain relative segments: jobs/copy.py, never ./x, ../x, ~/x, /x, a//b or a trailing /'
BUCKET_FORM = (
    'an S3 bucket name is 3-63 characters of lowercase letters, digits, dots and hyphens, '
    'starts and ends with a letter or digit, holds no doubled dot, and is not an IP address'
)


class PathSite(NamedTuple):
    """Where in a config one path-shaped value lives, and how it is named to a reader.

    Carried by `DeclaredPath` and `UnusableValue` alike, so `label` has one definition. An alias
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


class DeclaredName(NamedTuple):
    """One configured string that has to name a real AWS resource, `{env}` already substituted.

    A bucket name resolves to nothing on this machine and shapes no key, so it is neither of the
    two above. `join_uri` joins three strings and the other two are checked, which is what left
    this one able to reach the network before anything refused it."""

    site: PathSite
    value: str


class UnusableValue(NamedTuple):
    """One declared value that this config or this machine cannot supply, and why."""

    pipeline: str
    site: PathSite
    # Where the value resolved to, or None when it resolves to nothing on this machine: a
    # `script_prefix` is an S3 key prefix, and a glue job declaring no scripts has no value at
    # all. `Path('')` is `.`, so a non-optional field publishes a path the config never named.
    path: Path | None
    fault: ConfigFault
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
        if self.fault in SPELLING_FAULTS:
            return repr(self.configured)
        if self.path is not None:
            return str(self.path)
        return None

    def __str__(self) -> str:
        shown = self.shown
        line = f'{self.pipeline}: {self.site.label} {FAULT_WORDING[self.fault]}'
        return f'{line}: {shown}' if shown is not None else line


def home_fault(configured: str) -> ConfigFault | None:
    """UNRESOLVABLE_HOME when this string's `~user` names nobody on this machine.

    Asked at the same place `path_fault` is, so the diagnosis says which of the two happened.
    Left to `path_fault` alone, an unexpandable `~code/x` reports as `not found: ~code/x`,
    which sends the reader looking for a directory rather than at the missing slash."""
    try:
        Path(configured).expanduser()
    except RuntimeError:
        return ConfigFault.UNRESOLVABLE_HOME
    return None


DIRECTORY_KINDS = frozenset({PathKind.DIRECTORY, PathKind.NON_EMPTY_DIRECTORY})


def deployable_files(source: Path) -> list[Path]:
    """The files under `source` a deploy would send, bytecode caches excluded.

    One counter, read by `path_fault` and by `zip_lambda`, so `config validate` and the deploy
    cannot disagree about whether a directory holds anything to send."""
    return [found for found in sorted(source.rglob('*')) if found.is_file() and '__pycache__' not in found.parts]


def path_fault(path: Path, expects: PathKind) -> ConfigFault | None:
    """Why this path on disk cannot be used, or None when it can.

    An absent path and one present with the wrong type have opposite remedies — check the tree
    out, or fix the config key that names it — so they never share a name."""
    if not path.exists():
        return ConfigFault.ABSENT
    if expects in DIRECTORY_KINDS and not path.is_dir():
        return ConfigFault.EXPECTED_DIRECTORY
    if expects is PathKind.FILE and not path.is_file():
        return ConfigFault.EXPECTED_FILE
    if expects is PathKind.NON_EMPTY_DIRECTORY and not deployable_files(path):
        return ConfigFault.EMPTY_DIRECTORY
    return None


def key_fault(configured: str) -> ConfigFault | None:
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
        return ConfigFault.ESCAPES_ROOT
    if not all(segment and segment != '.' for segment in configured.split('/')):
        return ConfigFault.NOT_A_CLEAN_KEY
    return None


def recovery_lines(problems: list[UnusableValue]) -> list[str]:
    """What to tell the reader to do next, one line per kind of fault present.

    A spelling fault cannot be sent to `config show`: that command prints the *resolved* path,
    which has the `./`, the doubled separator and the trailing slash normalised out of it, so it
    renders a malformed key as a correct-looking one, and it shows a bucket name back exactly as
    written. Naming the form the value has to take is the answer to those; `config show` is the
    answer to a file that is not where the config says. A run carrying more than one kind gets a
    line for each, because dropping any of them sends those rows nowhere."""
    faults = {problem.fault for problem in problems}
    lines = []
    if faults & {ConfigFault.NOT_A_CLEAN_KEY, ConfigFault.ESCAPES_ROOT}:
        lines.append(KEY_FORM)
    if ConfigFault.NOT_A_BUCKET_NAME in faults:
        lines.append(BUCKET_FORM)
    if faults - SPELLING_FAULTS:
        lines.append('run "dectl config show" to see where each configured path resolves')
    return lines


# S3's own bucket naming rule, read off the service's documentation rather than off any check
# in this repo. Length, character set and the first and last character are the whole of it in
# one expression; the doubled dot and the IP-address form need their own arms below.
#
# Deliberately not shared with `FakeS3Client`, which encodes the same rule independently. A fake
# reading this constant would go green on whatever this expression gets wrong, which is the one
# input a test named for the property most needs to catch — CLAUDE.md § "A fake enforces the
# service's constraints". `test_the_fake_and_the_check_agree_on_bucket_names` pins the two
# together without joining them, so a drift in either is a red test rather than a silent one.
BUCKET_NAME = re.compile(r'[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]')
IP_ADDRESS = re.compile(r'\d{1,3}(\.\d{1,3}){3}')


def bucket_fault(configured: str) -> ConfigFault | None:
    """Why this configured string cannot name an S3 bucket, or None when it can.

    Answerable on any machine, so `config validate` asks it wherever the config is read.
    Reserved prefixes and suffixes are left out on purpose: AWS adds to that list, and a check
    refusing a name the service accepts is worse than one that misses a case, because the
    deploy it blocks would have worked."""
    if not BUCKET_NAME.fullmatch(configured) or '..' in configured or IP_ADDRESS.fullmatch(configured):
        return ConfigFault.NOT_A_BUCKET_NAME
    return None


def refuse_unusable_values(problems: list[UnusableValue]) -> None:
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
