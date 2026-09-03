from collections.abc import Mapping
from pathlib import Path
from pathlib import PurePosixPath
from typing import ClassVar
from typing import get_args

import yaml
from pyclisteno.paths import config_home
from pydantic import BaseModel
from pydantic import ConfigDict
from pydantic import ValidationError

from dectl.env import substitute_env
from dectl.output import error
from dectl.output import stderr_console
from dectl.values import EDIT_CONFIG
from dectl.values import ConfigFault
from dectl.values import DeclaredKey
from dectl.values import DeclaredName
from dectl.values import DeclaredPath
from dectl.values import DeclaresValues
from dectl.values import PathKind
from dectl.values import UnusableValue
from dectl.values import ValueSite
from dectl.values import bucket_fault
from dectl.values import expand_home
from dectl.values import home_fault
from dectl.values import key_fault
from dectl.values import leaves_root
from dectl.values import normalized
from dectl.values import path_fault
from dectl.values import root_fault

# `$XDG_CONFIG_HOME/dectl/config.yaml`, falling back to `~/.config` when the variable is unset.
# `config_home()` reads the environment at import, so a process that exports the variable after
# importing dectl keeps the path it started with.
CONFIG_DIR = config_home() / 'dectl'
CONFIG_PATH = CONFIG_DIR / 'config.yaml'

TEMPLATE_CONFIG = """\
defaults:
  account_id: ""
  region: us-east-2
  # Default environment when neither --env nor DECTL_ENV is given. The {env} token in any
  # name below is replaced with the active environment (dev/staging/prod/...).
  environment: dev
  aws_profile: ""

pipelines:
  example-pipeline:
    # The directory the relative paths below resolve from — every glue `scripts` entry and every
    # lambda `source_dir` — so a deploy reaches the same files from any directory. Absolute or
    # ~-rooted; a relative value is rejected.
    #
    # Left out, those paths resolve from the directory dectl is run in, so a deploy has to be run
    # from the checkout. Set it and `config validate` checks the directory and every path
    # resolving from it, which is why the example below stays commented.
    # resolve_paths_from: ~/code/example-pipeline
    glue_jobs:
      source-copy:
        name: my-{env}-source-copy-job
        script_bucket: my-script-bucket
        # The two halves of each script's S3 key, joined as written. Neither resolves against
        # resolve_paths_from — a prefix names nothing on disk, and a script's key comes from
        # this string rather than from where the file was found. Both are plain relative
        # segments: no leading ~ or /, no . or .. segment, no // and no trailing /.
        script_prefix: scripts
        scripts:
          - my-source-copy.py
        role: "arn:aws:iam::123456789012:role/my-{env}-glue-role"
        # Authoritative: a connection dropped from this list is detached on the next deploy.
        # Omit the key entirely to leave the job's connections alone.
        connections:
          - my-{env}-vpc-connection
        # Python shell: 0.0625 (1 GB) or 1 (16 GB). Omit for Spark jobs, which size by WorkerType.
        max_capacity: 1
        arguments:
          SOURCE_BUCKET: my-{env}-source-bucket
          SOURCE_PREFIX: incoming
    lambdas:
      my-function:
        name: my-{env}-lambda-function
        source_dir: modules/lambda/my_function/code
        live_alias: live
      my-workflow:
        name: my-{env}-durable-workflow
        source_dir: modules/lambda/my_workflow/code
        live_alias: live
        # Adds the durable execution verbs (executions, history) and qualifies every invoke,
        # which Lambda requires for durable functions.
        durable: true
    step_functions:
      my-flow:
        name: my-{env}-state-machine
        log_group: /aws/vendedlogs/states/my-{env}-state-machine
    buckets:
      raw: my-{env}-raw-data-bucket
      curated: my-{env}-curated-data-bucket
    iceberg_tables:
      # Glue Data Catalog database + table. dectl reads the table's own metadata file, so the
      # only requirement is that the table is registered in Glue with table_type ICEBERG.
      events:
        database: my-{env}-catalog
        table: events
    monitor:
      lambdas:
        - my-function
      step_functions:
        - my-flow
"""


class StrictModel(BaseModel):
    # Reject unknown keys everywhere so a typo (`step_function:` for `step_functions:`) is a
    # loud error via `config validate` rather than a silently ignored field. main.py catches the
    # resulting ValidationError at import so a bad config never bricks the CLI.
    model_config = ConfigDict(extra='forbid')


class ResourceModel(StrictModel, DeclaresValues):
    """One resource a pipeline holds, and which of its fields dectl resolves locally.

    `RESOURCE` and the three field declarations come from `DeclaresValues`, in `values.py`, so
    `env` can read them without importing this module.

    Every mechanism reading them walks `declaring_members`, which yields the pipeline and its
    resources alike. A field declared to some declarations and not the others is checked one way
    and not another, or excluded from the env-effect guard and never checked, and all of those
    read as success. `test_every_string_field_is_classified` is what makes a new field a
    decision, and `test_every_declaration_consumer_sees_every_member_shape` is what keeps a
    consumer from walking its own way to a narrower set."""


class Defaults(StrictModel):
    account_id: str
    region: str = 'us-east-2'
    environment: str = 'dev'
    aws_profile: str = ''


class JenkinsConfig(StrictModel):
    url: str
    user: str
    token: str


class JenkinsJobConfig(StrictModel):
    job_path: str
    parameters: dict[str, str] = {}


class GlueJobConfig(ResourceModel):
    RESOURCE: ClassVar[str] = 'glue'
    PATH_FIELDS: ClassVar[Mapping[str, PathKind]] = {'scripts': PathKind.FILE}
    # Both operands of the concatenation `join_key` performs. Checking only the one a bug was
    # found in leaves every shape it refuses passing silently in the other.
    KEY_FIELDS: ClassVar[frozenset[str]] = frozenset({'script_prefix', 'scripts'})
    # The third operand `join_uri` joins, and the one with no local meaning at all.
    BUCKET_FIELDS: ClassVar[frozenset[str]] = frozenset({'script_bucket'})

    name: str
    script_bucket: str
    script_prefix: str = 'scripts'
    scripts: list[str]
    role: str
    # Authoritative when set, so a connection removed here is detached from the job. None means
    # "dectl does not manage connections" and leaves whatever the job already has — a merge
    # instead would make a stale entry here immortal, silently reattaching a renamed connection
    # on every deploy.
    connections: list[str] | None = None
    arguments: dict[str, str] = {}
    # Python shell jobs accept 0.0625 (1 GB) or 1 (16 GB); Spark jobs use WorkerType instead.
    max_capacity: float | None = None


class LambdaConfig(ResourceModel):
    RESOURCE: ClassVar[str] = 'lambda'
    PATH_FIELDS: ClassVar[Mapping[str, PathKind]] = {'source_dir': PathKind.NON_EMPTY_DIRECTORY}

    name: str
    # Zipped whole and uploaded as bytes, with no key derived from it, so unlike a glue script
    # this may sit outside the pipeline's root as an absolute path.
    source_dir: str
    # The AWS Lambda alias (e.g. "live") that `deploy --publish` repoints to the new version.
    # Named live_alias, not alias, to avoid colliding with the CLI "alias" (the config key you type).
    live_alias: str | None = None
    # A durable function keeps a checkpointed execution log of its own, so it gets the extra
    # execution verbs (`executions`, `history`) and its invocations are qualified — Lambda rejects
    # an unqualified invoke of a durable function outright. Flagged in config rather than probed
    # at runtime so the command surface stays assembled at import, like every other resource.
    durable: bool = False


class StepFunctionConfig(ResourceModel):
    RESOURCE: ClassVar[str] = 'sfn'

    name: str
    # CloudWatch log group the state machine logs to. Required only to include this state
    # machine in `monitor`; `sfn logs` reads the execution history API and needs no log group.
    log_group: str = ''


class IcebergTableConfig(ResourceModel):
    RESOURCE: ClassVar[str] = 'iceberg'

    # An Iceberg table is addressed by the Glue Data Catalog pair that owns it. dectl reads its
    # state from the table's own metadata file, whose S3 location Glue stores on the table.
    database: str
    table: str


class MonitorConfig(ResourceModel):
    RESOURCE: ClassVar[str] = 'monitor'

    # Explicit selection of which resources `monitor` tails, by alias. Kept as its own block so
    # the monitored pipeline view is defined in one scannable place rather than inferred.
    lambdas: list[str] = []
    step_functions: list[str] = []


class PipelineConfig(ResourceModel):
    # The pipeline is not a resource held by alias, so it has no `RESOURCE` of its own — the
    # word below is what names it in an error and in `validate --json`.
    RESOURCE: ClassVar[str] = 'pipeline'
    PATH_FIELDS: ClassVar[Mapping[str, PathKind]] = {'resolve_paths_from': PathKind.DIRECTORY}
    # `buckets` is alias -> real bucket name, so each entry is a name to check and the alias is
    # what names it back to the reader.
    BUCKET_FIELDS: ClassVar[frozenset[str]] = frozenset({'buckets'})
    # `buckets` sits on the pipeline and holds no model of its own, but `s3` is the word the
    # CLI, `list` and `config show` give that collection, so it is the word its rows carry and
    # the word a caller scopes a door by.
    FIELD_RESOURCE: ClassVar[Mapping[str, str]] = {'buckets': 's3'}

    # Directory holding this pipeline's code. Every relative path in the block below — a lambda's
    # source_dir, a glue job's scripts — resolves against it, so a deploy targets the same files
    # from any working directory. Left unset they resolve against the process's working
    # directory, so a config that names none has to be run from the pipeline's checkout.
    resolve_paths_from: str | None = None
    glue_jobs: dict[str, GlueJobConfig] = {}
    lambdas: dict[str, LambdaConfig] = {}
    step_functions: dict[str, StepFunctionConfig] = {}
    # alias -> real S3 bucket name. The alias is what you reference on the CLI and what dectl
    # uses to build the exported shell variable / mount path (pipeline_alias).
    buckets: dict[str, str] = {}
    iceberg_tables: dict[str, IcebergTableConfig] = {}
    monitor: MonitorConfig = MonitorConfig()
    jenkins: JenkinsJobConfig | None = None


class DectlConfig(StrictModel):
    defaults: Defaults
    jenkins: JenkinsConfig | None = None
    pipelines: dict[str, PipelineConfig]


# The resource is `pipeline` because the key belongs to the pipeline block rather than to any
# resource in it, and the alias is empty for the same reason — so `label` is the config key on
# its own, which is what a reader typed and what they can grep the file for.
ROOT_SITE = ValueSite('pipeline', '', 'resolve_paths_from')


def pipeline_root(pipeline: PipelineConfig) -> Path:
    """The directory this pipeline's relative paths resolve against.

    `{env}` is substituted here, so a root under a per-environment directory joins a substituted
    `source_dir` without half of the result staying literal.

    Total, and it never exits. An unresolvable `~user` comes back as the literal string and is
    reported by `pipeline_value_faults` as `unresolvable_home`, because a refusal raised from
    here runs beneath a command that has already committed to emitting a document, and that
    leaves `validate --json` with an exit writing zero bytes."""
    if not pipeline.resolve_paths_from:
        return Path.cwd()
    declared = substitute_env(pipeline.resolve_paths_from)
    # A relative root is joined onto the working directory, which is where it actually lands.
    # `root_fault` refuses one and every deploy door reports it, but `config show` and
    # `list --json` render this without asking — and a relative path printed as a resolved one
    # is the confident wrong answer that command exists to prevent.
    #
    # A `~`-rooted value that expands to nothing is left as written. It was never relative, so
    # joining it onto the working directory would invent a directory nobody named; it is
    # reported as `unresolvable_home` and the reader needs to see what they typed.
    if declared.startswith('~'):
        return expand_home(declared)
    return Path.cwd() / expand_home(declared)


def resolve_from_root(pipeline: PipelineConfig, substituted_path: str) -> Path:
    """One of a pipeline's paths, as an absolute path, from a value already substituted.

    The `{env}` token is replaced by the caller, not here. Substituting in both places is a
    double substitution, and it agrees with a single one for every environment name that does
    not itself contain the token — so it is correct by a property of the value rather than by
    anything the code holds, and the two doors resolve different directories where it does not.
    `declared_paths` substitutes for the walk and both renderers; the deploy verbs substitute
    the field they read. The root's own token is resolved by `pipeline_root`.

    An entry that is already absolute is returned unchanged, which is what `Path.__truediv__`
    does with an absolute right-hand side. A `source_dir` may therefore sit outside the root. A
    glue `scripts` entry may not — `key_fault` refuses that, and `pipeline_value_faults` runs it
    from `config validate` and from the deploy, because the S3 key is built from the configured
    string rather than from the resolved path."""
    # Normalized, so what comes back is where the value lands rather than how it was written.
    # `modules/../code` names the same directory as `code`, and an unnormalized join asks the
    # filesystem about a `modules/` that need not exist — reporting the path absent when the
    # directory it names is right there.
    return normalized(pipeline_root(pipeline) / expand_home(substituted_path))


def as_values(configured: str | list[str]) -> list[str]:
    """A declared field's values, whether it holds one or a list of them."""
    return configured if isinstance(configured, list) else [configured]


def as_aliased_values(configured: str | list[str] | dict[str, str]) -> list[tuple[str, str]]:
    """A declared field's values paired with the alias that names each, where it has one.

    `buckets` is a mapping and every other declared field is a string or a list of them, so a
    bucket row can carry `s3/raw` while a `script_bucket` row carries the job's own alias."""
    if isinstance(configured, dict):
        return list(configured.items())
    return [('', value) for value in as_values(configured)]


def declared_resource_names() -> set[str]:
    """Every resource word a row can carry, read off the models rather than off one config.

    A fact about what dectl declares, not about what a file holds — so `glue` is a scope a
    caller may name whether or not the pipeline in front of it holds a job. Derived from the
    same `RESOURCE` and `FIELD_RESOURCE` the rows are labeled from, so a new kind joins this
    vocabulary by existing rather than by being listed here."""
    names = {PipelineConfig.RESOURCE, *PipelineConfig.FIELD_RESOURCE.values()}
    for field in PipelineConfig.model_fields.values():
        for annotation in [field.annotation, *get_args(field.annotation)]:
            if isinstance(annotation, type) and issubclass(annotation, DeclaresValues):
                names.add(annotation.RESOURCE)
                names.update(annotation.FIELD_RESOURCE.values())
    return names


def resource_members(pipeline: PipelineConfig) -> list[tuple[str, str, ResourceModel]]:
    """Each (collection, alias, resource) the pipeline holds, walked off the model.

    A resource kind added to `PipelineConfig` is enumerated here without this being edited, so
    the enumerations below cannot be missing one. A resource held by alias contributes its
    alias; one held as a bare field — `monitor` is the shape — contributes an empty alias, and
    a walk requiring a `dict` leaves any declaration on such a field inert, with nothing going red.

    `buckets` is a plain `dict[str, str]` and holds no model, so it is not reached here. The
    pipeline itself declares it, and `declaring_members` is what puts the pipeline in the
    walk beside its resources."""
    found = []
    for collection, value in pipeline:
        if isinstance(value, ResourceModel):
            found.append((collection, '', value))
        elif isinstance(value, dict):
            found.extend((collection, alias, member) for alias, member in value.items() if isinstance(member, ResourceModel))
    return found


def declaring_members(pipeline: PipelineConfig) -> list[tuple[str, str, ResourceModel]]:
    """Every carrier of a declaration in this pipeline: the pipeline itself, then its resources.

    The one walk. `declared_paths`, `declared_keys`, `declared_names`, `declares_nothing` and
    the scope guard all read this, so a member shape reaches every consumer or none of them.
    Each consumer filters by the declaration it cares about and by nothing else.

    A consumer that traverses for itself reaches a narrower set, and every way of being wrong
    about it is silent: a declaration the walk does not yield is a value nothing checks, and
    nothing checked reports no fault."""
    return [('', '', pipeline), *resource_members(pipeline)]


def declared_values(pipeline: PipelineConfig, declaration: str) -> list[tuple[ValueSite, ResourceModel, str]]:
    """Each (site, owner, field) the named declaration reaches, in walk order.

    `declaration` is the attribute holding the field names — `PATH_FIELDS`, `KEY_FIELDS` or
    `BUCKET_FIELDS`. The field's own values are left to the caller, because a path wants one
    row per entry and a bucket wants the alias its mapping keys it by."""
    sites = []
    for _, alias, member in declaring_members(pipeline):
        owner = type(member)
        for field in sorted(getattr(owner, declaration)):
            sites.append((ValueSite(owner.site_resource(field), alias, field), member, field))
    return sites


def declared_paths(pipeline: PipelineConfig) -> list[DeclaredPath]:
    """Every filesystem path this pipeline's config names, its own root included.

    The single enumeration of what is checked and resolved. `pipeline_value_faults` checks what
    it returns, `resolved_paths` resolves it for both renderers, and `aws_names_only` excludes
    it from the env-effect guard. Built from each model's `PATH_FIELDS`, so a path-bearing field
    is declared once beside the field itself rather than restated as a literal here.

    Values are substituted here rather than through `render_env_model`, which would also fire
    `warn_if_environment_had_no_effect` and put a warning in the middle of a validate run.

    An unwritten or empty value yields no row, because there is no path to check.
    `declares_nothing` is what reports it, and it reads the same walk, so the two cannot
    disagree about which fields exist."""
    paths = []
    for site, member, field in declared_values(pipeline, 'PATH_FIELDS'):
        configured = getattr(member, field)
        if configured is None:
            continue
        kind = type(member).PATH_FIELDS[field]
        for value in as_values(configured):
            if value:
                paths.append(DeclaredPath(site, substitute_env(value), kind))
    return paths


def declared_keys(pipeline: PipelineConfig) -> list[DeclaredKey]:
    """Every configured string a glue S3 key is built from.

    `join_key` joins two of them — the prefix and the script — so both are subject to the
    same spelling rules, and checking one operand and not the other leaves the second silent.
    A prefix names nothing on disk, so it is a key and not a path; a script is both, and
    appears here and in `declared_paths`."""
    keys = []
    for site, member, field in declared_values(pipeline, 'KEY_FIELDS'):
        for value in as_values(getattr(member, field)):
            keys.append(DeclaredKey(site, substitute_env(value)))
    return keys


def declared_names(pipeline: PipelineConfig) -> list[DeclaredName]:
    """Every configured string that has to name a real S3 bucket.

    `script_bucket` is the third operand `join_uri` joins, beside the prefix and the script that
    `declared_keys` covers. Each `buckets` entry is the same kind of value reached through a
    different verb — `s3 export`, `s3 ALIAS mount`, `s3 ALIAS uri` — and neither is a path, so
    neither resolves against anything.

    A field holding a mapping names its own aliases — `buckets` is `raw`, `staged` and the rest
    — and those win over the owner's alias, which for the pipeline is empty. `s3` as the
    resource comes from `FIELD_RESOURCE` rather than from a literal here, so the scope guard
    reads the same word these rows carry."""
    names = []
    for site, member, field in declared_values(pipeline, 'BUCKET_FIELDS'):
        for alias, value in as_aliased_values(getattr(member, field)):
            named = site._replace(alias=alias) if alias else site
            names.append(DeclaredName(named, substitute_env(value)))
    return names


def declares_nothing(pipeline: PipelineConfig) -> list[ValueSite]:
    """Every site whose declared path field holds nothing to check.

    A field declared in `PATH_FIELDS` and holding no value yields no `DeclaredPath`, so every
    check driven by that enumeration has nothing to look at. For a glue job that is the whole
    of its `ScriptLocation`, which `build_job_update` takes from `scripts[0]` — an empty list
    passes the schema and the deploy reaches the index.

    An empty *string* is the same absence and is worse resolved than reported. `Path('')` is
    `.`, which `/` drops, so an empty `source_dir` resolves to the anchor and the deploy ships
    whatever sits there as that function's code. A glue script is caught by `key_fault` on its
    way to an S3 key; `source_dir` is the declared path with no key behind it, so nothing else
    covers it.

    What is refused is the absence, not the reach. `source_dir: "."` resolves to the same
    directory and is accepted, because a writer who typed a dot said which directory they meant
    and a writer who left the key blank named none — the same line `None` sits on below. So the
    reach is not the reason: a repo whose root is one function's code is an ordinary config, and
    `deployable_files` drops `.git` and `__pycache__` from either spelling.

    Both are reported at the site rather than at the join, so `config validate` and the deploy
    say the same sentence and neither has to resolve a value to know it is absent.

    `None` is not empty, it is unwritten: an optional declared field such as
    `resolve_paths_from` says the key was left out, and leaving it out is the documented way to
    keep the earlier behavior. Only a key the writer put there and left blank is a fault.

    A field is reported only when *no* value in it is usable. A blank entry beside a real one is
    a different fault with a different remedy — delete the line, rather than name something to
    deploy — and it already has one, because an empty string is not a clean S3 key. Reporting
    the field as naming nothing while it names a script that exists says something untrue, and
    sends the reader to a command that shows them the surviving script."""
    empty = []
    for site, member, field in declared_values(pipeline, 'PATH_FIELDS'):
        configured = getattr(member, field)
        if configured is None:
            continue
        if not any(as_values(configured)):
            empty.append(site)
    return empty


def pipeline_value_faults(
    pipeline_name: str,
    pipeline: PipelineConfig,
    *,
    on_disk: bool,
    resource: str = '',
    alias: str = '',
) -> list[UnusableValue]:
    """Every declared path, key and bucket name of one pipeline that cannot be used, and why.

    Every door calls this — `config validate` over the whole config, a glue deploy over one job,
    a lambda deploy over one function — so a reader gets the same diagnosis whichever one they
    came in by. The scope is an argument, because the root row explains every row beneath it
    and a caller filtering the result afterwards drops exactly that one.

    The phases run in this order, and the order is load-bearing:

        1. the root's own spelling      returns alone; every path below it resolves through it
        2. declares_nothing             a field naming nothing, before anything reads its values
        3. key and bucket spelling      machine-independent, so they run whatever `on_disk` says
        4. leaving the anchor           reads `refused_values`; needs a root, so it is gated
        5. the root on disk             returns alone; an absent root explains every row beneath
        6. every other path on disk     reads `refused_values`; skipped when `on_disk` is false

    Two of them return rather than append, because a fault that explains every row beneath it is
    the only useful thing to say. Two read `refused_values`, so a value already refused for how
    it is written is not also reported as missing. Three ignore nothing and honor the scope; the
    root-on-disk phase honors no scope, because a caller asking about one job still cannot
    deploy it. A new fault class has to pick its point, and whether it reads `refused_values`.

    `on_disk` says whether this machine's files are part of the question, and it is required
    rather than defaulted because both answers are live and neither is the safe one. How a key
    is written is a property of the config and is answerable anywhere, so it is checked either
    way; whether a file is present is a property of this machine and is only meaningful once a
    root says where to look."""

    # A scope naming nothing returns no faults, and every door reads no faults as a clean
    # config and proceeds to deploy. Three spellings for one concept live in this module —
    # `GlueJobConfig.RESOURCE` is `glue`, its collection is `glue_jobs` — so `resource` is
    # checked against the declared set rather than trusted, and an alias against the pipeline's.
    #
    # The resource vocabulary comes from the models and the alias set from this pipeline's rows,
    # because the two questions differ. `glue` is a word a caller may name whether or not this
    # pipeline holds a job — reading it off the emitted rows makes a valid scope raise on a
    # pipeline declaring none of that kind. An alias is only ever this pipeline's.
    #
    # Both include `FIELD_RESOURCE`: a `buckets` row is labeled `s3` and its owner is the
    # pipeline, so a set walking members alone refuses the one word its own output uses.
    sites = [*(path.site for path in declared_paths(pipeline)), *(key.site for key in declared_keys(pipeline))]
    sites.extend(name.site for name in declared_names(pipeline))
    sites.extend(declares_nothing(pipeline))
    known_resources = declared_resource_names()
    if resource and resource not in known_resources:
        raise ValueError(f'no resource named {resource!r}; the declared resources are {sorted(known_resources)}')
    known_aliases = {site.alias for site in sites if site.alias}
    if alias and alias not in known_aliases:
        raise ValueError(f'no alias named {alias!r} in pipeline {pipeline_name!r}; declared: {sorted(known_aliases)}')

    def in_scope(site: ValueSite) -> bool:
        return (not resource or site.resource == resource) and (not alias or site.alias == alias)

    def row(site: ValueSite, path: Path | None, fault: ConfigFault, configured: str) -> UnusableValue:
        return UnusableValue(pipeline_name, site, path, fault, configured)

    def refused_values(found: list[UnusableValue]) -> set[tuple[ValueSite, str]]:
        """Which (site, value) pairs already carry a fault, so nothing reports one twice.

        Read at each point a new fault class is about to be added, because what counts as
        already-refused grows as the run goes on. Spelled inline at each of those points the
        reads look independent, and a fault class added between two of them produces a
        duplicate row rather than an error."""
        return {(problem.site, problem.configured) for problem in found}

    # Machine-independent, so these run whatever `on_disk` says and whatever the root turns out
    # to be. Gated on the root, declaring one on a box that never holds the checkout would
    # report less than declaring none.
    problems: list[UnusableValue] = []

    # First, because a root that is not rooted resolves against the working directory and every
    # path under it lands somewhere this cannot predict. Reported rather than raised at load: a
    # schema failure sends `main.py` to `cfg = None` and takes every pipeline command with it,
    # so the likeliest first spelling of a new key would blank the CLI that explains it.
    if pipeline.resolve_paths_from is not None:
        fault = root_fault(pipeline.resolve_paths_from)
        if fault:
            return [row(ROOT_SITE, None, fault, pipeline.resolve_paths_from)]

    nothing = declares_nothing(pipeline)
    problems += [row(site, None, ConfigFault.DECLARES_NOTHING, '') for site in nothing if in_scope(site)]

    # An empty value that is also a key operand is reported once, as the absence it is. Saying
    # both that it declares nothing and that it is not a clean key answers a question nobody
    # asked, and the spelling row is the one without the remedy in it. A `script_prefix` is a
    # key and not a path, so nothing above covers it and an empty one still reports here.
    declared_empty = set(nothing)
    for key in declared_keys(pipeline):
        if not key.value and key.site in declared_empty:
            continue
        fault = key_fault(key.value)
        if fault and in_scope(key.site):
            problems.append(row(key.site, None, fault, key.value))
    for name in declared_names(pipeline):
        fault = bucket_fault(name.value)
        if fault and in_scope(name.site):
            problems.append(row(name.site, None, fault, name.value))

    # Every value anchored to the root is checked for leaving it, not only the ones a key check
    # reaches for another reason. A lambda `source_dir` of `..` resolves to the parent of the
    # declared root, and the deploy uploads what it finds there — `.git` and a sibling checkout
    # included. An absolute or `~`-rooted value is exempt: it resolves to itself, so it never
    # claimed the anchor, and `source_dir` is documented as allowed to sit anywhere.
    #
    # Only where an anchor is declared. Without one the root is the working directory, a
    # relative `../shared/code` is an ordinary path that `zip_lambda` has always followed, and
    # refusing it fails a config that deploys. `key_fault` is untouched by that gate — an S3 key
    # is machine-independent and is checked whether or not this pipeline names a root.
    if pipeline.resolve_paths_from:
        already = refused_values(problems)
        root = pipeline_root(pipeline)
        for declared in declared_paths(pipeline):
            anchored = not declared.value.startswith('~') and not PurePosixPath(declared.value).is_absolute()
            if not anchored or not in_scope(declared.site):
                continue
            landed = resolve_from_root(pipeline, declared.value)
            if not leaves_root(root, landed) or (declared.site, declared.value) in already:
                continue
            # Carrying where it lands, because that is the answer: a reader who cannot see the
            # directory outside the root cannot tell a typo from a deliberate `..`.
            problems.append(row(declared.site, landed, ConfigFault.ESCAPES_ROOT, declared.value))
    if not on_disk:
        return problems

    # An absent root is the cause of every *file* fault beneath it, so nothing beneath it is
    # reported. A job with ten scripts otherwise prints ten lines that each name a file, and the
    # answer — that the checkout is not there — is in none of them. Never filtered by scope
    # either: a caller asking about one job still cannot deploy it, and this is why.
    for declared in declared_paths(pipeline):
        if declared.site != ROOT_SITE:
            continue
        root = pipeline_root(pipeline)
        # Asked before `path_fault`, and reported with no path, as the leaf rows report it. A
        # `~` naming nobody resolves to nothing at either site, and one row publishing the
        # literal string in the `path` key gives a `--json` consumer branching on `path is None`
        # two answers to one `fault` value. The written form is what the reader needs anyway:
        # the missing slash is the finding, and `shown` reaches it through `SPELLING_FAULTS`.
        if fault := home_fault(declared.value):
            return [*problems, row(ROOT_SITE, None, fault, declared.value)]
        if fault := path_fault(root, declared.expects):
            return [*problems, row(ROOT_SITE, root, fault, declared.value)]

    # A value whose spelling is already refused is not also reported as missing. Resolution
    # would place `./copy.py` somewhere real and `jobs//copy.py` somewhere else, and a second
    # row about the wrong question buries the one that has the remedy in it. Keyed on the value
    # as well as the site, so one bad script does not silence the check on its siblings.
    refused = refused_values(problems)
    for declared in declared_paths(pipeline):
        if declared.site == ROOT_SITE or not in_scope(declared.site) or (declared.site, declared.value) in refused:
            continue
        if fault := home_fault(declared.value):
            problems.append(row(declared.site, None, fault, declared.value))
            continue
        resolved = resolve_from_root(pipeline, declared.value)
        if fault := path_fault(resolved, declared.expects):
            problems.append(row(declared.site, resolved, fault, declared.value))
    return problems


def config_value_faults(config: DectlConfig) -> list[UnusableValue]:
    """Every declared path, key and bucket name in the config that cannot be used.

    A pipeline naming no root is still checked for how its keys are written, which is
    answerable on any machine. Its files are not: they resolve against whatever directory dectl
    is run from, so their absence right here says nothing about whether a deploy run from the
    checkout would find them, and reporting it would fail `validate` on a config that is
    correct.

    Checking the root directory alone would answer half the question. A root that exists and
    holds none of the source the pipeline names is present and useless, and the deploy is where
    that surfaces otherwise."""
    problems = []
    for name, pipeline in config.pipelines.items():
        problems.extend(pipeline_value_faults(name, pipeline, on_disk=bool(pipeline.resolve_paths_from)))
    return problems


def load_config() -> DectlConfig | None:
    if not CONFIG_PATH.exists():
        return None
    raw = yaml.safe_load(CONFIG_PATH.read_text())
    return DectlConfig.model_validate(raw)


# The two ways a config that exists still fails to load: YAML that will not parse, and YAML that
# does not match the models. Declared once because every caller catches them together — a reader
# hitting either one needs the same answer, and `except Exception` would swallow real bugs.
CONFIG_LOAD_ERRORS = (yaml.YAMLError, ValidationError)

# A rejected value is shown so the reader can see which key the location names, but a `missing`
# error's input is the whole parent object and a large pipeline block would bury the message.
MAX_INPUT_CHARS = 160


def config_error_outcome(exc: yaml.YAMLError | ValidationError) -> str:
    """Which of the two load failures this is, as `validate --json` publishes it.

    The same discriminator the headline reads, so the machine reader and the human reader are
    told apart the same way. Folding both into one outcome leaves the `--json` caller — the one
    who cannot see the stderr sentence — unable to tell a file YAML could not parse from one
    that parsed and named the wrong keys, and those have different fixes."""
    return 'invalid_yaml' if isinstance(exc, yaml.YAMLError) else 'invalid_schema'


def config_error_headline(exc: yaml.YAMLError | ValidationError) -> str:
    """The one-line summary: which file, and which of the two failures it hit."""
    kind = 'is not valid YAML' if isinstance(exc, yaml.YAMLError) else 'does not match the expected schema'
    return f'config at {CONFIG_PATH} {kind}:'


def describe_config_error(exc: yaml.YAMLError | ValidationError) -> list[str]:
    """The failure as indented detail lines: where it is, what is wrong, and what was rejected.

    The rejected value is carried because it is what identifies the offending key when the
    location alone is ambiguous — `pipelines.p.step_function` names a plausible spelling, and
    the value is the block the reader has to go and find. Pydantic's error code and docs URL
    are dropped, since both restate the message in the line above them.
    """
    if isinstance(exc, ValidationError):
        lines = []
        for err in exc.errors():
            location = '.'.join(str(part) for part in err['loc']) or '(root)'
            rejected = repr(err['input'])
            if len(rejected) > MAX_INPUT_CHARS:
                rejected = f'{rejected[:MAX_INPUT_CHARS]}…'
            lines.append(f'  {location}: {err["msg"]}')
            lines.append(f'    got: {rejected}')
        return lines
    return [f'  {line}' for line in str(exc).splitlines()]


def report_config_error(exc: yaml.YAMLError | ValidationError) -> None:
    """Print the whole diagnostic, headline through fix hint.

    One renderer for every caller, because the question is the same from `config validate`,
    `config show`, `list`, `search`, an unknown pipeline name, and bare `dectl`. A site that
    answers it its own way answers it worse: the reader reaches for whichever command they
    happened to think of, and the useful one is not knowable from outside.

    Every line goes to stderr: the detail is not the answer to a read, and `config show --json`
    has to stay parseable on the path where it fails. The details print with markup disabled
    because a rejected list value renders as `['a']`, which rich would eat as a style tag.
    """
    error(config_error_headline(exc))
    for line in describe_config_error(exc):
        stderr_console.print(line, markup=False)
    # The headline already named the file, and repeating a long path here wraps across two
    # lines on a narrow terminal, which breaks it mid-token for anyone copying it.
    stderr_console.print(EDIT_CONFIG)


def init_config() -> Path:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(TEMPLATE_CONFIG)
    return CONFIG_PATH
