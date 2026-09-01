from pathlib import Path

import yaml
from pyclisteno.paths import config_home
from pydantic import BaseModel
from pydantic import ConfigDict
from pydantic import ValidationError
from pydantic import field_validator

from dectl.output import error
from dectl.output import stderr_console

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
    # Where this pipeline's code lives. The relative paths below — every glue `scripts` entry and
    # every lambda `source_dir` — resolve against it, so a deploy reaches the same files from any
    # directory. Absolute or ~-rooted; a relative value is rejected.
    #
    # Left out, those paths resolve against the directory dectl is run from, so a deploy has to
    # be run from the checkout. Set it and `config validate` checks the directory and every path
    # resolving against it, which is why the example below stays commented.
    # repo: ~/code/example-pipeline
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


class LambdaConfig(StrictModel):
    name: str
    source_dir: str
    # The AWS Lambda alias (e.g. "live") that `deploy --publish` repoints to the new version.
    # Named live_alias, not alias, to avoid colliding with the CLI "alias" (the config key you type).
    live_alias: str | None = None
    # A durable function keeps a checkpointed execution log of its own, so it gets the extra
    # execution verbs (`executions`, `history`) and its invocations are qualified — Lambda rejects
    # an unqualified invoke of a durable function outright. Flagged in config rather than probed
    # at runtime so the command surface stays assembled at import, like every other resource.
    durable: bool = False


class StepFunctionConfig(StrictModel):
    name: str
    # CloudWatch log group the state machine logs to. Required only to include this state
    # machine in `monitor`; `sfn watch` reads the execution history API and needs no log group.
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
    # from any working directory. Where no repo is set, they resolve against the process's
    # working directory, so a config that names none has to be run from the pipeline's checkout.
    repo: str | None = None
    glue_jobs: dict[str, GlueJobConfig] = {}
    lambdas: dict[str, LambdaConfig] = {}
    step_functions: dict[str, StepFunctionConfig] = {}
    # alias -> real S3 bucket name. The alias is what you reference on the CLI and what dectl
    # uses to build the exported shell variable / mount path (pipeline_alias).
    buckets: dict[str, str] = {}
    iceberg_tables: dict[str, IcebergTableConfig] = {}
    monitor: MonitorConfig = MonitorConfig()
    jenkins: JenkinsJobConfig | None = None

    @field_validator('repo')
    @classmethod
    def repo_is_rooted(cls, value: str | None) -> str | None:
        """Reject a relative repo, which would reintroduce the dependency the key removes.

        `~` counts as rooted because expansion makes it absolute. Existence is deliberately not
        checked here: a config is loaded on every invocation, including on a machine that holds
        none of the checkouts, and a read of an AWS resource needs no local file. `config
        validate` is where the directory is required to actually be there."""
        if value is None:
            return value
        if not Path(value).expanduser().is_absolute():
            raise ValueError(f'must be an absolute or ~-rooted path; {value!r} resolves against the working directory')
        return value


class DectlConfig(StrictModel):
    defaults: Defaults
    jenkins: JenkinsConfig | None = None
    pipelines: dict[str, PipelineConfig]


def pipeline_root(pipeline: PipelineConfig) -> Path:
    """The directory this pipeline's relative paths resolve against."""
    if pipeline.repo:
        return Path(pipeline.repo).expanduser()
    return Path.cwd()


def resolve_in_repo(pipeline: PipelineConfig, relative: str) -> Path:
    """One of a pipeline's configured paths, as an absolute path.

    An entry that is already absolute is returned unchanged, which is what `Path.__truediv__`
    does with an absolute right-hand side. So a config may mix a repo-relative `source_dir` with
    an absolute one and both land where they read."""
    return pipeline_root(pipeline) / Path(relative).expanduser()


def missing_declared_paths(config: DectlConfig) -> list[str]:
    """Every path a repo-declaring pipeline names that is not on this machine.

    A pipeline without a repo is skipped rather than reported. Its paths resolve against whatever
    directory dectl is run from, so their absence right here says nothing about whether a deploy
    run from the checkout would find them, and reporting it would fail `validate` on a config
    that is correct.

    Checking the repo directory alone would answer half the question. A repo that exists and
    holds none of the source the pipeline names is present and useless, and the deploy is where
    that surfaces today."""
    problems = []
    for name, pipeline in config.pipelines.items():
        if not pipeline.repo:
            continue
        root = pipeline_root(pipeline)
        if not root.is_dir():
            problems.append(f'{name}: repo not found: {root}')
            continue
        for alias, job in pipeline.glue_jobs.items():
            problems.extend(
                f'{name}: glue/{alias} script not found: {resolve_in_repo(pipeline, script)}'
                for script in job.scripts
                if not resolve_in_repo(pipeline, script).is_file()
            )
        problems.extend(
            f'{name}: lambda/{alias} source_dir not found: {resolve_in_repo(pipeline, fn.source_dir)}'
            for alias, fn in pipeline.lambdas.items()
            if not resolve_in_repo(pipeline, fn.source_dir).is_dir()
        )
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
    stderr_console.print('run "dectl config edit" to fix it', style='dim')


def init_config() -> Path:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(TEMPLATE_CONFIG)
    return CONFIG_PATH
