from pathlib import Path

import yaml
from pydantic import BaseModel

CONFIG_DIR = Path.home() / '.config' / 'dectl'
CONFIG_PATH = CONFIG_DIR / 'config.yaml'

TEMPLATE_CONFIG = """\
defaults:
  account_id: ""
  region: us-east-2
  environment: dev
  aws_profile: ""

pipelines:
  example-pipeline:
    glue_jobs:
      source-copy:
        name: my-dev-source-copy-job
        script_bucket: my-script-bucket
        script_prefix: scripts
        scripts:
          - my-source-copy.py
        role: "arn:aws:iam::123456789012:role/my-glue-role"
        arguments:
          SOURCE_BUCKET: my-source-bucket
          SOURCE_PREFIX: incoming
    lambdas:
      my-function:
        name: my-dev-lambda-function
        source_dir: modules/lambda/my_function/code
    buckets:
      raw: my-dev-raw-data-bucket
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
    arguments: dict[str, str] = {}


class LambdaConfig(BaseModel):
    name: str
    source_dir: str


class BucketConfig(BaseModel):
    raw: str = ''
    curated: str = ''
    error: str = ''


class PipelineConfig(BaseModel):
    glue_jobs: dict[str, GlueJobConfig] = {}
    lambdas: dict[str, LambdaConfig] = {}
    buckets: BucketConfig = BucketConfig()
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
