from typing import Any

from dectl.config import PipelineConfig
from dectl.config import pipeline_root
from dectl.config import resolve_in_repo
from dectl.env import substitute_env
from dectl.env import warn_if_environment_had_no_effect
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
    if pipeline.iceberg_tables:
        types.append('iceberg')
    return types


def pipeline_to_dict(name: str, pipeline: PipelineConfig) -> dict[str, Any]:
    """Build the stable --json shape for a pipeline: alias -> resolved AWS name per resource.

    Names are env-substituted so the JSON reflects the active environment, matching what the
    human view prints. This is the documented schema for `list --json` / `config show --json`."""
    warn_if_environment_had_no_effect(pipeline.model_dump(exclude={'repo'}))
    return {
        'pipeline': name,
        # The resolved directory, with `~` already expanded, plus whether the config named it.
        # An undeclared repo still resolves — to the working directory — and a reader who cannot
        # tell that apart from a declared one cannot tell what a deploy would reach.
        'repo': {'path': str(pipeline_root(pipeline)), 'declared': pipeline.repo is not None},
        'glue': {
            alias: {
                'name': substitute_env(job.name),
                'script_bucket': substitute_env(job.script_bucket),
                'script_prefix': job.script_prefix,
                'scripts': [substitute_env(script) for script in job.scripts],
                'script_paths': [str(resolve_in_repo(pipeline, script)) for script in job.scripts],
            }
            for alias, job in pipeline.glue_jobs.items()
        },
        'lambda': {
            alias: {
                'name': substitute_env(fn.name),
                'durable': fn.durable,
                'source_dir': substitute_env(fn.source_dir),
                'source_path': str(resolve_in_repo(pipeline, fn.source_dir)),
            }
            for alias, fn in pipeline.lambdas.items()
        },
        'sfn': {
            alias: {'name': substitute_env(sfn.name), 'log_group': substitute_env(sfn.log_group)}
            for alias, sfn in pipeline.step_functions.items()
        },
        's3': {alias: {'bucket': substitute_env(bucket)} for alias, bucket in pipeline.buckets.items()},
        'iceberg': {
            alias: {'database': substitute_env(table.database), 'table': substitute_env(table.table)}
            for alias, table in pipeline.iceberg_tables.items()
        },
    }


def render_pipeline(name: str, pipeline: PipelineConfig) -> None:
    """Print a pipeline's resources as human-readable, env-substituted alias -> AWS name lines."""
    warn_if_environment_had_no_effect(pipeline.model_dump(exclude={'repo'}))
    types = resource_types(pipeline)
    info(f'[bold]{name}[/bold] ({", ".join(types) or "none configured"})')
    source = '' if pipeline.repo else ' [dim](working directory — no repo set)[/dim]'
    info(f'  repo: {pipeline_root(pipeline)}{source}')
    for alias, job in pipeline.glue_jobs.items():
        info(f'  glue/{alias}: {substitute_env(job.name)}')
        info(f'    bucket: s3://{substitute_env(job.script_bucket)}/{job.script_prefix}/')
        # The resolved path, not the config string: the reader is asking which file a deploy
        # would upload, and the config string does not answer that on its own.
        for script in job.scripts:
            info(f'      {resolve_in_repo(pipeline, script)}')
    for alias, fn in pipeline.lambdas.items():
        # Worth calling out inline: a durable function carries a different set of verbs.
        marker = ' (durable)' if fn.durable else ''
        info(f'  lambda/{alias}: {substitute_env(fn.name)}{marker}')
        info(f'    source: {resolve_in_repo(pipeline, fn.source_dir)}')
    for alias, sfn in pipeline.step_functions.items():
        info(f'  sfn/{alias}: {substitute_env(sfn.name)}')
    for alias, bucket in pipeline.buckets.items():
        info(f'  s3/{alias}: {substitute_env(bucket)}')
    for alias, table in pipeline.iceberg_tables.items():
        info(f'  iceberg/{alias}: {substitute_env(table.database)}.{substitute_env(table.table)}')
    info('')
