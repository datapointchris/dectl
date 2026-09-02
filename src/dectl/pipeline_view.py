from typing import Any

from dectl.config import ROOT_SITE
from dectl.config import PipelineConfig
from dectl.config import declared_paths
from dectl.config import pipeline_root
from dectl.config import resolve_from_root
from dectl.config import resource_members
from dectl.env import aws_names_of
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
    environment. `env.aws_names_of` answers the same question one level down, for the resource a
    verb resolves, and both read the model's own declaration — the pipeline's root included."""
    dumped = aws_names_of(pipeline)
    for collection, alias, member in resource_members(pipeline):
        dumped[collection][alias] = aws_names_of(member)
    return dumped


def resolved_paths(pipeline: PipelineConfig) -> dict[tuple[str, str], dict[str, list[str]]]:
    """Every declared path of this pipeline, resolved, keyed by resource and alias then field.

    Both renderers read this rather than walking the resource dicts themselves, so a field added
    to a model's `PATH_FIELDS` is resolved *and displayed* without either of them being edited.
    Keying the field as well is what makes the display derived — `configuration.md` § "Every
    tool prints its resolved config": a row is guaranteed by something that runs, not by
    remembering to add one.

    The outer key is the pair rather than a `resource/alias` string, so nothing rebuilds what a
    `PathSite` holds and an alias carrying a slash cannot collide with a different resource."""
    grouped: dict[tuple[str, str], dict[str, list[str]]] = {}
    for declared in declared_paths(pipeline):
        if declared.site == ROOT_SITE:
            continue
        fields = grouped.setdefault((declared.site.resource, declared.site.alias), {})
        fields.setdefault(declared.site.field, []).append(str(resolve_from_root(pipeline, declared.value)))
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
        # config named it. A pipeline that named none still resolves — to the working directory
        # — and a reader who cannot tell that apart cannot say what a deploy reaches.
        'resolve_paths_from': {
            'path': str(pipeline_root(pipeline)),
            'declared': pipeline.resolve_paths_from is not None,
        },
        'glue': {
            alias: {
                'name': substitute_env(job.name),
                'script_bucket': substitute_env(job.script_bucket),
                'script_prefix': substitute_env(job.script_prefix),
                'scripts': [substitute_env(script) for script in job.scripts],
                'paths': resolved.get(('glue', alias), {}),
            }
            for alias, job in pipeline.glue_jobs.items()
        },
        'lambda': {
            alias: {
                'name': substitute_env(fn.name),
                'durable': fn.durable,
                'source_dir': substitute_env(fn.source_dir),
                'paths': resolved.get(('lambda', alias), {}),
            }
            for alias, fn in pipeline.lambdas.items()
        },
        'sfn': {
            alias: {
                'name': substitute_env(sfn.name),
                'log_group': substitute_env(sfn.log_group),
                'paths': resolved.get(('sfn', alias), {}),
            }
            for alias, sfn in pipeline.step_functions.items()
        },
        's3': {alias: {'bucket': substitute_env(bucket)} for alias, bucket in pipeline.buckets.items()},
        'iceberg': {
            alias: {
                'database': substitute_env(table.database),
                'table': substitute_env(table.table),
                'paths': resolved.get(('iceberg', alias), {}),
            }
            for alias, table in pipeline.iceberg_tables.items()
        },
    }


def render_pipeline(name: str, pipeline: PipelineConfig) -> None:
    """Print a pipeline's resources as human-readable, env-substituted alias -> AWS name lines."""
    warn_if_environment_had_no_effect(aws_names_only(pipeline))
    resolved = resolved_paths(pipeline)
    types = resource_types(pipeline)
    info(f'[bold]{name}[/bold] ({", ".join(types) or "none configured"})')
    source = '' if pipeline.resolve_paths_from else ' (working directory — resolve_paths_from is unset)'
    info(f'  resolve_paths_from: {pipeline_root(pipeline)}{source}')

    def print_paths(resource: str, alias: str) -> None:
        """The resolved path of every field this resource declares, named by its config key.

        The reader is asking which file a deploy would send, and the config string does not
        answer that. Driven off `resolved_paths`, so a newly declared field prints here without
        this function being edited."""
        for field, paths in resolved.get((resource, alias), {}).items():
            for path in paths:
                info(f'    {field}: {path}')

    for alias, job in pipeline.glue_jobs.items():
        info(f'  glue/{alias}: {substitute_env(job.name)}')
        info(f'    bucket: s3://{substitute_env(job.script_bucket)}/{substitute_env(job.script_prefix)}/')
        print_paths('glue', alias)
    for alias, fn in pipeline.lambdas.items():
        # Worth calling out inline: a durable function carries a different set of verbs.
        marker = ' (durable)' if fn.durable else ''
        info(f'  lambda/{alias}: {substitute_env(fn.name)}{marker}')
        print_paths('lambda', alias)
    for alias, sfn in pipeline.step_functions.items():
        info(f'  sfn/{alias}: {substitute_env(sfn.name)}')
        print_paths('sfn', alias)
    for alias, bucket in pipeline.buckets.items():
        info(f'  s3/{alias}: {substitute_env(bucket)}')
    for alias, table in pipeline.iceberg_tables.items():
        info(f'  iceberg/{alias}: {substitute_env(table.database)}.{substitute_env(table.table)}')
        print_paths('iceberg', alias)
    info('')
