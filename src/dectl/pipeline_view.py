from typing import Any

from dectl.config import PipelineConfig
from dectl.config import declared_paths
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


def aws_names_only(pipeline: PipelineConfig) -> dict[str, Any]:
    """The pipeline's config with every local path field removed.

    `warn_if_environment_had_no_effect` asks whether `--env` changed an AWS name. A `{env}` in a
    local path satisfies `contains_env_placeholder` without naming anything in AWS, so leaving
    one in silences the warning for a pipeline whose bucket and job names all hardcode an
    environment. Driven from `declared_paths`, so a new path field is excluded by being declared
    rather than by being remembered here."""
    dumped = pipeline.model_dump(exclude={'repo'})
    for declared in declared_paths(pipeline):
        if declared.resource == 'glue':
            dumped.get('glue_jobs', {}).get(declared.alias, {}).pop(declared.field + 's', None)
        elif declared.resource == 'lambda':
            dumped.get('lambdas', {}).get(declared.alias, {}).pop(declared.field, None)
    return dumped


def resolved_paths(pipeline: PipelineConfig) -> dict[str, list[str]]:
    """Every declared path of this pipeline, resolved, keyed by `<resource>/<alias>`.

    Both renderers read this rather than walking the resource dicts themselves, so a field added
    to `declared_paths` is displayed without either of them being edited."""
    grouped: dict[str, list[str]] = {}
    for declared in declared_paths(pipeline):
        if declared.resource == 'repo':
            continue
        key = f'{declared.resource}/{declared.alias}'
        grouped.setdefault(key, []).append(str(resolve_in_repo(pipeline, declared.value)))
    return grouped


def pipeline_to_dict(name: str, pipeline: PipelineConfig) -> dict[str, Any]:
    """Build the stable --json shape for a pipeline: alias -> resolved AWS name per resource.

    Names are env-substituted so the JSON reflects the active environment, matching what the
    human view prints. This is the documented schema for `list --json` / `config show --json`."""
    warn_if_environment_had_no_effect(aws_names_only(pipeline))
    resolved = resolved_paths(pipeline)
    return {
        'pipeline': name,
        # The resolved directory, with `~` expanded and `{env}` substituted, plus whether the
        # config named it. An undeclared repo still resolves — to the working directory — and a
        # reader who cannot tell that apart from a declared one cannot say what a deploy reaches.
        'repo': {'path': str(pipeline_root(pipeline)), 'declared': pipeline.repo is not None},
        'glue': {
            alias: {
                'name': substitute_env(job.name),
                'script_bucket': substitute_env(job.script_bucket),
                'script_prefix': substitute_env(job.script_prefix),
                'scripts': [substitute_env(script) for script in job.scripts],
                'script_paths': resolved[f'glue/{alias}'],
            }
            for alias, job in pipeline.glue_jobs.items()
        },
        'lambda': {
            alias: {
                'name': substitute_env(fn.name),
                'durable': fn.durable,
                'source_dir': substitute_env(fn.source_dir),
                'source_path': resolved[f'lambda/{alias}'][0],
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
    warn_if_environment_had_no_effect(aws_names_only(pipeline))
    resolved = resolved_paths(pipeline)
    types = resource_types(pipeline)
    info(f'[bold]{name}[/bold] ({", ".join(types) or "none configured"})')
    source = '' if pipeline.repo else ' (working directory — no repo set)'
    info(f'  repo: {pipeline_root(pipeline)}{source}')
    for alias, job in pipeline.glue_jobs.items():
        info(f'  glue/{alias}: {substitute_env(job.name)}')
        info(f'    bucket: s3://{substitute_env(job.script_bucket)}/{substitute_env(job.script_prefix)}/')
        # The resolved path, not the config string: the reader is asking which file a deploy
        # would upload, and the config string does not answer that on its own.
        for path in resolved.get(f'glue/{alias}', []):
            info(f'      {path}')
    for alias, fn in pipeline.lambdas.items():
        # Worth calling out inline: a durable function carries a different set of verbs.
        marker = ' (durable)' if fn.durable else ''
        info(f'  lambda/{alias}: {substitute_env(fn.name)}{marker}')
        for path in resolved.get(f'lambda/{alias}', []):
            info(f'    source_dir: {path}')
    for alias, sfn in pipeline.step_functions.items():
        info(f'  sfn/{alias}: {substitute_env(sfn.name)}')
    for alias, bucket in pipeline.buckets.items():
        info(f'  s3/{alias}: {substitute_env(bucket)}')
    for alias, table in pipeline.iceberg_tables.items():
        info(f'  iceberg/{alias}: {substitute_env(table.database)}.{substitute_env(table.table)}')
    info('')
