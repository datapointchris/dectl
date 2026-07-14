from pathlib import Path

import yaml
from pydantic import BaseModel

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
        arguments:
          SOURCE_BUCKET: my-{env}-source-bucket
          SOURCE_PREFIX: incoming
    lambdas:
      my-function:
        name: my-{env}-lambda-function
        source_dir: modules/lambda/my_function/code
        alias: live
    step_functions:
      my-flow:
        name: my-{env}-state-machine
        log_group: /aws/vendedlogs/states/my-{env}-state-machine
    buckets:
      raw: my-{env}-raw-data-bucket
      curated: my-{env}-curated-data-bucket
    monitor:
      lambdas:
        - my-function
      step_functions:
        - my-flow
"""


class Defaults(BaseModel):
    account_id: str
    region: str = 'us-east-2'
    environment: str = 'dev'
    aws_profile: str = ''


class JenkinsConfig(BaseModel):
    url: str
    user: str
    token: str


class JenkinsJobConfig(BaseModel):
    job_path: str
    parameters: dict[str, str] = {}


class GlueJobConfig(BaseModel):
    name: str
    script_bucket: str
    script_prefix: str = 'scripts'
    scripts: list[str]
    role: str
    connections: list[str] = []
    arguments: dict[str, str] = {}


class LambdaConfig(BaseModel):
    name: str
    source_dir: str
    alias: str | None = None


class StepFunctionConfig(BaseModel):
    name: str
    # CloudWatch log group the state machine logs to. Required only to include this state
    # machine in `monitor`; `sfn watch` reads the execution history API and needs no log group.
    log_group: str = ''


class MonitorConfig(BaseModel):
    # Explicit selection of which resources `monitor` tails, by alias. Kept as its own block so
    # the monitored pipeline view is defined in one scannable place rather than inferred.
    lambdas: list[str] = []
    step_functions: list[str] = []


class PipelineConfig(BaseModel):
    glue_jobs: dict[str, GlueJobConfig] = {}
    lambdas: dict[str, LambdaConfig] = {}
    step_functions: dict[str, StepFunctionConfig] = {}
    # shortname -> real S3 bucket name. The shortname is what you reference on the CLI and
    # what dectl uses to build the exported shell variable / mount path (pipeline_shortname).
    buckets: dict[str, str] = {}
    monitor: MonitorConfig = MonitorConfig()
    jenkins: JenkinsJobConfig | None = None


class DectlConfig(BaseModel):
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
