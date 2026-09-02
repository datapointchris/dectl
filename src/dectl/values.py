"""Whether a value the config names outside the program can be what it has to be.

Three questions, and they are not the same question. `path_fault` asks whether a resolved
`Path` is on this machine and is the right kind of thing. `key_fault` asks whether a string is
spelled in a way S3 will store as written. `bucket_fault` asks whether a string can name an S3
bucket at all. A glue script answers the first two — it is uploaded from disk and named by a
key built from the config string. A lambda `source_dir` answers only the first, a glue
`script_prefix` only the second, and a bucket name only the third, so `config.py` enumerates
them as three records.

One vocabulary reports all three: `ConfigFault` is the name a caller branches on and
`validate --json` publishes. Keeping the records separate is what gives a value that answers
only one of the three somewhere to be declared.

Nothing here knows what a pipeline is. The models and the walk over them are `config.py`'s.
"""

import re
from collections.abc import Mapping
from enum import StrEnum
from pathlib import Path
from pathlib import PurePosixPath
from typing import ClassVar
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
    KEY_ESCAPES_ROOT = 'key_escapes_root'
    NOT_A_BUCKET_NAME = 'not_a_bucket_name'


# The faults about how a value is spelled rather than about what this machine holds. They are
# reported against the configured string: resolution normalises away the very thing a key fault
# names, and a bucket name resolves to nothing at all.
#
# `ESCAPES_ROOT` is deliberately absent. A path that climbs out of the anchor resolves to a real
# directory, and where it lands is the answer — so its row carries the resolved path and its
# reader is sent to `config show`, which is the command that prints it.
SPELLING_FAULTS = frozenset({ConfigFault.NOT_A_CLEAN_KEY, ConfigFault.KEY_ESCAPES_ROOT, ConfigFault.NOT_A_BUCKET_NAME})

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
    ConfigFault.ESCAPES_ROOT: 'climbs out of the directory paths resolve from',
    ConfigFault.KEY_ESCAPES_ROOT: 'climbs out of the directory paths resolve from, and the S3 key is built from it',
    ConfigFault.NOT_A_BUCKET_NAME: 'is not a name S3 will accept for a bucket',
}


def join_key(prefix: str, name: str) -> str:
    """The two halves of an S3 key, joined as written.

    Joined rather than normalised: S3 stores `a//b` and `a/./b` as themselves, which is why
    `key_fault` reads the raw string. Both operands reach this already substituted, or both raw
    for the help panel that runs before `--env` is parsed."""
    return f'{prefix}/{name}'


def s3_uri(bucket: str, key: str) -> str:
    """How an S3 location is spelled, from the bucket and the key.

    The one place the shape is written. Five callers name a location — the upload's own report,
    `ScriptLocation`, `--extra-py-files`, the per-alias help panel and `config show` — and each
    spelling it out is a rendering that can drift from the object the deploy actually wrote.
    They pass different operands on purpose: the help panel is built before `--env` is parsed
    and passes raw ones."""
    return f's3://{bucket}/{key}'


# The form each spelling-checked value has to take, said once per kind. An error naming what is
# wrong without naming what is right leaves the reader to guess, and these are the faults where
# `config show` cannot help: it prints the resolved path, which has the malformation normalised
# out of it, and it shows a bucket name back exactly as written.
KEY_FORM = 'an S3 key is written as plain relative segments: jobs/copy.py, never ./x, ../x, ~/x, /x, a//b or a trailing /'
# Said for a value anchored to `resolve_paths_from` that leaves it. `~/x` and `/x` are legal
# here and refused by `KEY_FORM`, so the two cannot share a line: a `source_dir` told to use the
# key form is told to avoid the one spelling that fixes it.
ANCHOR_FORM = (
    'a path anchored to resolve_paths_from stays inside it: name a directory below the root, '
    'or write an absolute or ~-rooted path, which resolves on its own and is not anchored'
)
BUCKET_FORM = (
    'an S3 bucket name is 3-63 characters of lowercase letters, digits, dots and hyphens, '
    'starts and ends with a letter or digit, holds no doubled dot, and is not an IP address'
)


class DeclaresValues:
    """A config model that says which of its fields name something outside the program.

    `PATH_FIELDS` maps a field to what it has to be on disk, `KEY_FIELDS` names the fields a
    glue S3 key is built from, and `BUCKET_FIELDS` the fields that have to be bucket names. A
    field can be in more than one: a glue script is uploaded from disk *and* is the second
    operand of its key, which is why it is an AWS name as well as a local path.

    Declared here rather than on the config model so `env` can import it. Reaching for these by
    name with a `getattr` default let a model that declared nothing and a model whose
    declaration failed to arrive look identical, and the difference showed up as a missing
    warning three modules away."""

    # The word the CLI, the error text and `validate --json` use for this kind of resource. It
    # lives beside the declarations because every row they emit carries it.
    RESOURCE: ClassVar[str] = ''
    PATH_FIELDS: ClassVar[Mapping[str, PathKind]] = {}
    KEY_FIELDS: ClassVar[frozenset[str]] = frozenset()
    BUCKET_FIELDS: ClassVar[frozenset[str]] = frozenset()
    # A declared field whose rows a reader knows by another name than its owner's `RESOURCE`.
    # `buckets` sits on the pipeline and is `s3` to the CLI, to `list` and to `config show`.
    # Declared rather than spelled in the walk that emits it, so the scope guard and the
    # renderers read the same answer the rows carry.
    FIELD_RESOURCE: ClassVar[Mapping[str, str]] = {}

    @classmethod
    def site_resource(cls, field: str) -> str:
        """The resource name a row for this field carries."""
        return cls.FIELD_RESOURCE.get(field, cls.RESOURCE)

    @classmethod
    def local_only_fields(cls) -> frozenset[str]:
        """The declared fields that name nothing in AWS.

        What the env-effect guard drops. A path that is also a key operand or a bucket name is
        an AWS name — `--env` changing it changes the object written — so it stays in the dump
        the guard reads."""
        return frozenset(cls.PATH_FIELDS) - cls.KEY_FIELDS - cls.BUCKET_FIELDS


class ValueSite(NamedTuple):
    """Where in a config one externally-named value lives, and how it is named to a reader.

    A path, an S3 key operand and a bucket name all sit somewhere, so this carries all three
    rather than the first. Carried by every `Declared*` record and by `UnusableValue` alike, so
    `label` has one definition. An alias holding a space is what separates these three fields
    from a `label` a consumer splits."""

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

    site: ValueSite
    value: str
    expects: PathKind


class DeclaredKey(NamedTuple):
    """One configured string an S3 key is built from, with `{env}` already substituted.

    A `script_prefix` shapes a key and names nothing on disk, so it is one of these and not a
    `DeclaredPath`. A glue script is both."""

    site: ValueSite
    value: str


class DeclaredName(NamedTuple):
    """One configured string that has to name a real AWS resource, `{env}` already substituted.

    A bucket name resolves to nothing on this machine and shapes no key, so it is neither of the
    two above. `join_uri` joins three strings and the other two are checked, which is what left
    this one able to reach the network before anything refused it."""

    site: ValueSite
    value: str


class UnusableValue(NamedTuple):
    """One declared value that this config or this machine cannot supply, and why."""

    pipeline: str
    site: ValueSite
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


def is_deployable(source: Path, found: Path) -> bool:
    """Whether one entry under `source` is a file a deploy would send.

    Exclusions are tested against the path *below* `source`, never the absolute one. Reading the
    whole path puts every directory above the checkout into the question, so a source under
    anything named `__pycache__` — a scratch tree, a container mount — reports as holding
    nothing to deploy, and `config validate` calls a directory with a handler in it empty."""
    return found.is_file() and '__pycache__' not in found.relative_to(source).parts


def deployable_files(source: Path) -> list[Path]:
    """Every file under `source` a deploy would send, in a stable order.

    `zip_lambda` needs the list. `path_fault` needs only whether there is one, which is
    `has_deployable_files` — building and sorting a whole tree to answer yes or no costs 70ms
    on a 7,000-file source against 0.4ms, and `config validate` reads a config file."""
    return sorted(found for found in source.rglob('*') if is_deployable(source, found))


def has_deployable_files(source: Path) -> bool:
    """Whether `source` holds anything a deploy would send, stopping at the first one."""
    return any(is_deployable(source, found) for found in source.rglob('*'))


def escapes_root(configured: str) -> bool:
    """Whether this value climbs out of the directory it resolves from.

    A `..` segment is the whole of it for a value already known to be relative. A leading `~`
    or an absolute path leave the root as well, but they are legal for a value nothing derives
    a key from — a lambda `source_dir` may sit anywhere — so the caller decides which arms
    apply. `key_fault` runs all three; the path walk runs this one over relative values."""
    return '..' in configured.split('/')


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
    if expects is PathKind.NON_EMPTY_DIRECTORY and not has_deployable_files(path):
        return ConfigFault.EMPTY_DIRECTORY
    return None


def key_fault(configured: str) -> ConfigFault | None:
    """Why this configured string cannot become an S3 key, or None when it can.

    Every segment has to be a plain name, tested as such. `PurePosixPath` normalises `.`, `//`
    and a trailing slash away — the very shapes S3 stores literally — so anything routed
    through it agrees with the raw string on every input that matters.

    A leading `~` escapes the root the way an absolute path does. `PurePosixPath` calls it
    relative and holds no `..`, so both of the other arms miss it, and it resolves through
    `$HOME` — the same dependence on where the deploy ran that `resolve_paths_from` exists to
    remove, arriving by a different route.

    Checked here rather than in a field validator on purpose. A schema failure blanks the whole
    pipeline tree, because `main.py` falls back to `cfg = None` and every pipeline command
    disappears with it. A config carrying a malformed key still loads, `config validate` names
    it and the deploy refuses it, so the diagnosis reaches the reader without the CLI that
    delivers it going away."""
    if configured.startswith('~') or PurePosixPath(configured).is_absolute() or escapes_root(configured):
        return ConfigFault.KEY_ESCAPES_ROOT
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
    line for each, because dropping any of them sends those rows nowhere.

    Climbing out of the anchor gets its own line rather than the key one. The two subjects want
    opposite things: an S3 key may not be `~/x` or `/x`, and those are exactly the spellings that
    fix an anchored directory, so one line for both tells half its readers to avoid their own
    remedy."""
    faults = {problem.fault for problem in problems}
    lines = []
    if faults & {ConfigFault.NOT_A_CLEAN_KEY, ConfigFault.KEY_ESCAPES_ROOT}:
        lines.append(KEY_FORM)
    if ConfigFault.ESCAPES_ROOT in faults:
        lines.append(ANCHOR_FORM)
    if ConfigFault.NOT_A_BUCKET_NAME in faults:
        lines.append(BUCKET_FORM)
    if faults & SPELLING_FAULTS:
        # A form says what the value has to look like and no more. Every other fault names a
        # command, so a spelling-only run would be the one that names none.
        lines.append('run "dectl config edit" to fix it')
    if faults - SPELLING_FAULTS:
        lines.append('run "dectl config show" to see where each configured path resolves')
    return lines


def render_unusable_values(problems: list[UnusableValue]) -> list[str]:
    """Every unusable value as a line, then what to do about them.

    The whole message as data, so a door that has to emit a document rather than exit — `config
    validate --json` — reads the same lines the deploy doors print. Splitting the rendering from
    the exit is what stops the second door rebuilding this loop and drifting from it."""
    return [str(problem) for problem in problems] + recovery_lines(problems)


# S3's own bucket naming rule, read off the service's documentation. Length, character set and
# the first and last character are the whole of it in one expression; the doubled dot, the
# IP-address form and the reserved affixes need their own arms below.
#
# `FakeS3Client` refuses an upload by calling `bucket_fault`, so the rule is written once. What
# keeps that honest is `BUCKET_NAMES` in the tests: a table of accepted and refused names
# written from S3's documentation rather than from this code, and measured against both. A
# second hand-written copy of the expression inside the fake could not disagree with this one on
# any input, so it proved nothing while reading as an independent reading.
BUCKET_NAME = re.compile(r'[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]')
IP_ADDRESS = re.compile(r'\d{1,3}(\.\d{1,3}){3}')
# Affixes S3 reserves for its own addressing forms. A bucket cannot be created with any of them,
# so refusing one here never blocks a deploy that would have worked.
RESERVED_BUCKET_PREFIXES = ('xn--', 'sthree-', 'amzn-s3-demo-')
RESERVED_BUCKET_SUFFIXES = ('-s3alias', '--ol-s3', '--x-s3')


def bucket_fault(configured: str) -> ConfigFault | None:
    """Why this configured string cannot name an S3 bucket, or None when it can.

    Answerable on any machine, so `config validate` asks it wherever the config is read.

    The reserved affixes are refused. The direction that costs something is refusing a name S3
    accepts, because the deploy that blocks would have worked — and these cannot do that, since
    S3 refuses every one of them itself. A list that goes stale as AWS reserves more errs the
    harmless way: dectl accepts the name and S3 is the one that says no."""
    if not BUCKET_NAME.fullmatch(configured) or '..' in configured or IP_ADDRESS.fullmatch(configured):
        return ConfigFault.NOT_A_BUCKET_NAME
    if configured.startswith(RESERVED_BUCKET_PREFIXES) or configured.endswith(RESERVED_BUCKET_SUFFIXES):
        return ConfigFault.NOT_A_BUCKET_NAME
    return None


def refuse_unusable_values(problems: list[UnusableValue]) -> None:
    """Print every unusable value and exit, or return when there are none.

    The one reporter, and the one place in the path domain that exits — so a glue deploy, a
    lambda deploy and `config validate` say the same sentence about the same fault, and nothing
    beneath a command that has committed to emitting a document can cut it short."""
    if not problems:
        return
    for line in render_unusable_values(problems):
        error(line)
    raise typer.Exit(1)
