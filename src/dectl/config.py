from pathlib import Path
from pathlib import PurePosixPath

import yaml
from pyclisteno.paths import config_home
from pydantic import BaseModel
from pydantic import ConfigDict
from pydantic import ValidationError
from pydantic import field_validator

from dectl.env import ENV_PLACEHOLDER
from dectl.env import substitute_env
from dectl.output import error
from dectl.output import stderr_console
from dectl.paths import DeclaredKey
from dectl.paths import DeclaredPath
from dectl.paths import PathKind
from dectl.paths import PathSite
from dectl.paths import UnusablePath
from dectl.paths import expand_home_or_exit
from dectl.paths import expands_at_load
from dectl.paths import key_fault
from dectl.paths import path_fault

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


class GlueJobConfig(StrictModel):
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

    @field_validator('scripts')
    @classmethod
    def scripts_expand(cls, value: list[str]) -> list[str]:
        """Reject only what cannot be turned into a path at all.

        How a script may be *written* — relative, no `..`, no `.` or doubled separator — is
        `key_fault`, checked by `config validate` and by the deploy rather than here. A schema
        failure blanks the whole pipeline tree, which is out of proportion to a key that is
        merely malformed, and it takes away the commands that would report it."""
        for script in value:
            expands_at_load(script)
        return value


class LambdaConfig(StrictModel):
    name: str
    # Zipped whole and uploaded as bytes, with no key derived from it, so unlike a glue script
    # this may sit outside the pipeline repo as an absolute path.
    source_dir: str
    # The AWS Lambda alias (e.g. "live") that `deploy --publish` repoints to the new version.
    # Named live_alias, not alias, to avoid colliding with the CLI "alias" (the config key you type).
    live_alias: str | None = None
    # A durable function keeps a checkpointed execution log of its own, so it gets the extra
    # execution verbs (`executions`, `history`) and its invocations are qualified — Lambda rejects
    # an unqualified invoke of a durable function outright. Flagged in config rather than probed
    # at runtime so the command surface stays assembled at import, like every other resource.
    durable: bool = False

    @field_validator('source_dir')
    @classmethod
    def source_dir_expands(cls, value: str) -> str:
        """Reject a `~user` that names nobody, testing a token-free stand-in.

        `--env` is parsed after the config loads, so the substituted form is not available here.
        `render_env_model` re-validates with the real value inside every verb, and that is where
        a `~{env}` resolving to a missing user is caught."""
        expands_at_load(value)
        return value


class StepFunctionConfig(StrictModel):
    name: str
    # CloudWatch log group the state machine logs to. Required only to include this state
    # machine in `monitor`; `sfn logs` reads the execution history API and needs no log group.
    log_group: str = ''


class IcebergTableConfig(StrictModel):
    # An Iceberg table is addressed by the Glue Data Catalog pair that owns it. dectl reads its
    # state from the table's own metadata file, whose S3 location Glue stores on the table.
    database: str
    table: str


class MonitorConfig(StrictModel):
    # Explicit selection of which resources `monitor` tails, by alias. Kept as its own block so
    # the monitored pipeline view is defined in one scannable place rather than inferred.
    lambdas: list[str] = []
    step_functions: list[str] = []


class PipelineConfig(StrictModel):
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

    @field_validator('resolve_paths_from')
    @classmethod
    def root_is_rooted(cls, value: str | None) -> str | None:
        """Reject a relative root, which would reintroduce the dependency the key removes.

        `~` counts as rooted because expansion makes it absolute. Existence is deliberately not
        checked here: a config is loaded on every invocation, including on a machine that holds
        none of the checkouts, and a read of an AWS resource needs no local file. `config
        validate` is where the directory is required to actually be there.

        The `{env}` token is stripped before the test rather than substituted, because the
        active environment is not known at load — `--env` is parsed after this runs. A token
        can only appear inside a path, never at its head, so it cannot change rootedness."""
        if value is None:
            return value
        expands_at_load(value)
        if not (value.startswith('~') or PurePosixPath(value.replace(ENV_PLACEHOLDER, 'x')).is_absolute()):
            raise ValueError(f'must be an absolute or ~-rooted path; {value!r} resolves against the working directory')
        return value


class DectlConfig(StrictModel):
    defaults: Defaults
    jenkins: JenkinsConfig | None = None
    pipelines: dict[str, PipelineConfig]


# The resource is `pipeline` because the key belongs to the pipeline block rather than to any
# resource in it, and the alias is empty for the same reason — so `label` is the config key on
# its own, which is what a reader typed and what they can grep the file for.
ROOT_SITE = PathSite('pipeline', '', 'resolve_paths_from')


def pipeline_root(pipeline: PipelineConfig) -> Path:
    """The directory this pipeline's relative paths resolve against.

    `{env}` is substituted here, so a root under a per-environment directory joins a substituted
    `source_dir` without half of the result staying literal."""
    if not pipeline.resolve_paths_from:
        return Path.cwd()
    return expand_home_or_exit(substitute_env(pipeline.resolve_paths_from))


def resolve_from_root(pipeline: PipelineConfig, configured_path: str) -> Path:
    """One of a pipeline's configured paths, as an absolute path.

    Substitution is applied to both halves and is idempotent, so a caller passing an already
    rendered value gets the same answer as one passing the raw config string.

    An entry that is already absolute is returned unchanged, which is what `Path.__truediv__`
    does with an absolute right-hand side. A `source_dir` may therefore sit outside the repo. A
    glue `scripts` entry may not — `key_fault` refuses that, and `pipeline_path_faults` runs it
    from `config validate` and from the deploy, because the S3 key is built from the configured
    string rather than from the resolved path."""
    return pipeline_root(pipeline) / expand_home_or_exit(substitute_env(configured_path))


def declared_paths(pipeline: PipelineConfig) -> list[DeclaredPath]:
    """Every filesystem path this pipeline's config names, its own root included.

    The single enumeration of what is checked and resolved. `pipeline_path_faults` checks what
    it returns, `resolved_paths` resolves it for both renderers, and `aws_names_only` excludes
    it from the env-effect guard. A path-bearing field added here is checked, resolved and
    excluded at once; one added anywhere else is checked and never resolved, or resolved and
    never checked.

    Values are substituted here rather than through `render_env_model`, which would also fire
    `warn_if_environment_had_no_effect` and put a warning in the middle of a validate run."""
    paths = []
    if pipeline.resolve_paths_from:
        paths.append(DeclaredPath(ROOT_SITE, '', substitute_env(pipeline.resolve_paths_from), PathKind.DIRECTORY))
    paths.extend(
        DeclaredPath(PathSite('glue', alias, 'scripts'), 'glue_jobs', substitute_env(script), PathKind.FILE)
        for alias, job in pipeline.glue_jobs.items()
        for script in job.scripts
    )
    paths.extend(
        DeclaredPath(PathSite('lambda', alias, 'source_dir'), 'lambdas', substitute_env(fn.source_dir), PathKind.DIRECTORY)
        for alias, fn in pipeline.lambdas.items()
    )
    return paths


def declared_keys(pipeline: PipelineConfig) -> list[DeclaredKey]:
    """Every configured string a glue S3 key is built from.

    `script_key` joins two of them — the prefix and the script — so both are subject to the
    same spelling rules, and checking only the operand a bug was found in leaves the other
    silent. A prefix names nothing on disk, so it is a key and not a path; a script is both,
    and appears here and in `declared_paths`."""
    keys = []
    for alias, job in pipeline.glue_jobs.items():
        keys.append(DeclaredKey(PathSite('glue', alias, 'script_prefix'), substitute_env(job.script_prefix)))
        keys.extend(DeclaredKey(PathSite('glue', alias, 'scripts'), substitute_env(script)) for script in job.scripts)
    return keys


def pipeline_path_faults(
    pipeline_name: str,
    pipeline: PipelineConfig,
    *,
    on_disk: bool = True,
    resource: str = '',
    alias: str = '',
) -> list[UnusablePath]:
    """Every declared path and key of one pipeline that cannot be used, with the reason.

    Every door calls this — `config validate` over the whole config, a glue deploy over one job,
    a lambda deploy over one function — so a reader gets the same diagnosis whichever one they
    came in by. The scope is an argument rather than something a caller filters afterwards: a
    caller filtering the result drops the root row, which is the one fault that explains every
    other row it kept.

    `on_disk=False` keeps the machine-independent half. How a key is written is a property of
    the config and is answerable anywhere; whether a file is present is a property of this
    machine and is only meaningful once a root says where to look."""

    def in_scope(site: PathSite) -> bool:
        return (not resource or site.resource == resource) and (not alias or site.alias == alias)

    # An absent root is the cause of every path fault beneath it, so it is reported alone. A job
    # with ten scripts otherwise prints ten lines that each name a file, and the answer — that
    # the checkout is not there — is in none of them. Never filtered by scope either: a caller
    # asking about one job still cannot deploy it, and this is why.
    if on_disk and pipeline.resolve_paths_from:
        root = pipeline_root(pipeline)
        root_fault = path_fault(root, PathKind.DIRECTORY)
        if root_fault:
            return [UnusablePath(pipeline_name, ROOT_SITE, root, root_fault, pipeline.resolve_paths_from)]

    problems = [
        UnusablePath(pipeline_name, key.site, Path(key.value), fault, key.value)
        for key in declared_keys(pipeline)
        if in_scope(key.site) and (fault := key_fault(key.value))
    ]
    # A value whose spelling is already refused is not also reported as missing. Resolution
    # would place `./copy.py` somewhere real and `jobs//copy.py` somewhere else, and a second
    # row about the wrong question buries the one that has the remedy in it. Keyed on the value
    # as well as the site, so one bad script does not silence the check on its siblings.
    refused = {(problem.site, problem.configured) for problem in problems}
    if not on_disk:
        return problems
    for declared in declared_paths(pipeline):
        if not in_scope(declared.site) or (declared.site, declared.value) in refused:
            continue
        resolved = resolve_from_root(pipeline, declared.value)
        fault = path_fault(resolved, declared.expects)
        if fault:
            problems.append(UnusablePath(pipeline_name, declared.site, resolved, fault, declared.value))
    return problems


def config_path_faults(config: DectlConfig) -> list[UnusablePath]:
    """Every declared path and key in the config that this config or machine cannot supply.

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
        problems.extend(pipeline_path_faults(name, pipeline, on_disk=bool(pipeline.resolve_paths_from)))
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
    stderr_console.print('run "dectl config edit" to fix it')


def init_config() -> Path:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(TEMPLATE_CONFIG)
    return CONFIG_PATH
