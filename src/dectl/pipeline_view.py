from typing import Any

from dectl.config import ROOT_SITE
from dectl.config import PipelineConfig
from dectl.config import declared_paths
from dectl.config import pipeline_root
from dectl.config import resolve_from_root
from dectl.config import resource_members
from dectl.env import aws_names_of
from dectl.env import warn_if_environment_had_no_effect
from dectl.output import info
from dectl.resource_kinds import RESOURCE_KINDS
from dectl.resource_kinds import AliasPaths


def resource_types(pipeline: PipelineConfig) -> list[str]:
    """The resource kinds this pipeline declares, in display order."""
    return [kind.word for kind in RESOURCE_KINDS if kind.declared(pipeline)]


def aws_names_only(pipeline: PipelineConfig) -> dict[str, Any]:
    """The pipeline's config with every local path field removed.

    `warn_if_environment_had_no_effect` asks whether `--env` changed an AWS name. A `{env}` in a
    local path satisfies `contains_env_placeholder` without naming anything in AWS, so leaving
    one in silences the warning for a pipeline whose bucket and job names all hardcode an
    environment. `env.aws_names_of` answers the same question one level down, for the resource a
    verb resolves, and both read the model's own declaration — the pipeline's root included.

    A resource held as a bare field rather than by alias — `monitor` is the shape — replaces its
    collection instead of being filed under an alias inside it. Filing it under the empty alias
    leaves the unfiltered original beside the filtered copy, and the guard reads the original,
    so a `{env}` in a local path goes on silencing the warning it exists to raise."""
    dumped = aws_names_of(pipeline)
    # Already substituted into the names below, and a {env} in one names nothing on its own.
    dumped.pop('variables', None)
    for collection, alias, member in resource_members(pipeline):
        if alias:
            dumped[collection][alias] = aws_names_of(member)
        else:
            dumped[collection] = aws_names_of(member)
    return dumped


def resolved_paths(pipeline: PipelineConfig) -> dict[str, AliasPaths]:
    """Every declared path of this pipeline, resolved, keyed by resource word, then alias, then field.

    Both renderers read this rather than walking the resource dicts themselves, so a path field
    added to a model is resolved *and displayed* without either of them being edited. Keying the
    field as well is what makes that row derived: it is guaranteed by something that runs rather
    than by someone remembering to add one.

    Each kind's sections read the share under its word. The alias is a key of its own rather
    than part of a `resource/alias` string, so an alias carrying a slash cannot collide with a
    different resource."""
    grouped: dict[str, AliasPaths] = {}
    for declared in declared_paths(pipeline):
        if declared.site == ROOT_SITE:
            continue
        fields = grouped.setdefault(declared.site.resource, {}).setdefault(declared.site.alias, {})
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
        **{kind.word: kind.json_section(pipeline, resolved.get(kind.word, {})) for kind in RESOURCE_KINDS},
        # Aliases into this pipeline rather than AWS names, so nothing here is substituted. It
        # decides which log groups are tailed, and a reader could see that nowhere else.
        'monitor': pipeline.monitor.model_dump(),
    }


def render_pipeline(name: str, pipeline: PipelineConfig) -> None:
    """Print a pipeline's resources as human-readable, env-substituted alias -> AWS name lines.

    The lines are printed as one text rather than one call each. rich parses markup, highlights
    and wraps per call, and that setup was most of `list` and `config show` across many
    pipelines. The output is byte-identical either way."""
    warn_if_environment_had_no_effect(aws_names_only(pipeline))
    lines: list[str] = []
    resolved = resolved_paths(pipeline)
    types = resource_types(pipeline)
    lines.append(f'[bold]{name}[/bold] ({", ".join(types) or "none configured"})')
    # `is not None`, matching `pipeline_to_dict`'s `declared`. Truthiness calls an empty string
    # unset, and the two renderers then disagree about one fact — with the human one sending a
    # reader to add a key their file already has.
    declared = pipeline.resolve_paths_from is not None
    source = '' if declared else ' (working directory — resolve_paths_from is unset)'
    lines.append(f'  resolve_paths_from: {pipeline_root(pipeline)}{source}')

    for kind in RESOURCE_KINDS:
        kind_paths = resolved.get(kind.word, {})
        for row in kind.view_rows(pipeline):
            label = f'{kind.word}/{row.alias}' if row.alias else kind.word
            lines.append(f'  {label}: {row.headline}')
            lines.extend(f'    {detail}' for detail in row.details)
            # The resolved path of every field this resource declares, named by its config key.
            # The reader is asking which file a deploy would send, and the config string does
            # not answer that.
            lines.extend(f'    {field}: {path}' for field, paths in kind_paths.get(row.alias, {}).items() for path in paths)
    # Aliases into the rows above rather than AWS names, so nothing here is substituted.
    monitored = pipeline.monitor.selected_aliases()
    if monitored:
        lines.append(f'  monitor: {", ".join(monitored)}')
    lines.append('')
    info('\n'.join(lines))
