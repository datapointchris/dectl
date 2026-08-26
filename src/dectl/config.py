from pathlib import Path

import yaml
from pydantic import BaseModel
from pydantic import ConfigDict

CONFIG_DIR = Path.home() / '.config' / 'dectl'
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


def load_config() -> DectlConfig | None:
    if not CONFIG_PATH.exists():
        return None
    raw = yaml.safe_load(CONFIG_PATH.read_text())
    return DectlConfig.model_validate(raw)


def init_config() -> Path:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(TEMPLATE_CONFIG)
    return CONFIG_PATH
