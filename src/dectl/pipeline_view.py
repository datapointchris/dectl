from typing import Any

from dectl.config import PipelineConfig
from dectl.env import substitute_env
from dectl.output import info


def resource_types(pipeline: PipelineConfig) -> list[str]:
    """The resource kinds this pipeline defines, in display order."""
    types = []
    if pipeline.glue_jobs:
        types.append('glue')
    if pipeline.lambdas:
        types.append('lambda')
    if pipeline.step_functions:
        types.append('sfn')
    if pipeline.buckets:
        types.append('s3')
    return types


def pipeline_to_dict(name: str, pipeline: PipelineConfig) -> dict[str, Any]:
    """Build the stable --json shape for a pipeline: alias -> resolved AWS name per resource.

    Names are env-substituted so the JSON reflects the active environment, matching what the
    human view prints. This is the documented schema for `list --json` / `config show --json`."""
    return {
        'pipeline': name,
        'glue': {
            alias: {
                'name': substitute_env(job.name),
                'script_bucket': substitute_env(job.script_bucket),
                'script_prefix': job.script_prefix,
                'scripts': job.scripts,
            }
            for alias, job in pipeline.glue_jobs.items()
        },
        'lambda': {alias: {'name': substitute_env(fn.name)} for alias, fn in pipeline.lambdas.items()},
        'sfn': {
            alias: {'name': substitute_env(sfn.name), 'log_group': substitute_env(sfn.log_group)}
            for alias, sfn in pipeline.step_functions.items()
        },
        's3': {alias: {'bucket': substitute_env(bucket)} for alias, bucket in pipeline.buckets.items()},
    }


def render_pipeline(name: str, pipeline: PipelineConfig) -> None:
    """Print a pipeline's resources as human-readable, env-substituted alias -> AWS name lines."""
    types = resource_types(pipeline)
    info(f'[bold]{name}[/bold] ({", ".join(types) or "none configured"})')
    for alias, job in pipeline.glue_jobs.items():
        info(f'  glue/{alias}: {substitute_env(job.name)}')
        info(f'    bucket: s3://{substitute_env(job.script_bucket)}/{job.script_prefix}/')
        for script in job.scripts:
            info(f'      {script}')
    for alias, fn in pipeline.lambdas.items():
        info(f'  lambda/{alias}: {substitute_env(fn.name)}')
    for alias, sfn in pipeline.step_functions.items():
        info(f'  sfn/{alias}: {substitute_env(sfn.name)}')
    for alias, bucket in pipeline.buckets.items():
        info(f'  s3/{alias}: {substitute_env(bucket)}')
    info('')
