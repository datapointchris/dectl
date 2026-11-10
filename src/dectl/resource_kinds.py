"""Each kind of resource a pipeline holds, declared once, and how `list` shows each one.

The command tree, the root help's summary, `reference`, `resource_types` and both `list`
renderers iterate `RESOURCE_KINDS`. A kind is one entry here beside its command module. Its word
is its model's `RESOURCE`, or `FIELD_RESOURCE`'s for `buckets`, which holds no model.

`monitor` is no kind. It selects lambdas and state machines declared here, and its one command
spans them, so `make_pipeline_app` and the renderers place it themselves."""

from collections.abc import Callable
from typing import Any
from typing import NamedTuple

import typer

from dectl.commands.glue import make_glue_app
from dectl.commands.iceberg import make_iceberg_app
from dectl.commands.jenkins import make_jenkins_app
from dectl.commands.lambda_ import make_lambda_app
from dectl.commands.s3 import make_s3_app
from dectl.commands.stepfunctions import make_sfn_app
from dectl.config import DectlConfig
from dectl.config import GlueJobConfig
from dectl.config import IcebergTableConfig
from dectl.config import JenkinsJobConfig
from dectl.config import LambdaConfig
from dectl.config import PipelineConfig
from dectl.config import StepFunctionConfig
from dectl.env import substitute_env
from dectl.values import join_uri

# One kind's resolved paths: alias -> config key -> each path the key names.
AliasPaths = dict[str, dict[str, list[str]]]


class ViewRow(NamedTuple):
    """One `list` row: `KIND/ALIAS: headline`, or `KIND: headline` for a kind held without an alias.

    The renderer indents `details` under it, then the resolved path of each field the resource
    declares."""

    alias: str
    headline: str
    details: list[str]


class ResourceKind(NamedTuple):
    """One kind of resource, and everything the command tree and `list` need from it."""

    # The command, the `--json` key and the label of each `list` row.
    word: str
    # The `PipelineConfig` field holding the kind, which is empty or None where none is declared.
    collection: str
    make_app: Callable[[str, PipelineConfig, DectlConfig], typer.Typer]
    json_section: Callable[[PipelineConfig, AliasPaths], Any]
    view_rows: Callable[[PipelineConfig], list[ViewRow]]
    panel: str = 'Resources'
    # The names the root help lists after the word, where they are not the aliases.
    names: Callable[[PipelineConfig], list[str]] | None = None
    # What `config.yaml` has to hold before the kind has a command, beyond the pipeline's block.
    needs: Callable[[DectlConfig], bool] | None = None

    def declared(self, pipeline: PipelineConfig) -> bool:
        return bool(getattr(pipeline, self.collection))

    def attached(self, pipeline: PipelineConfig, config: DectlConfig) -> bool:
        """Whether the pipeline has this kind's command: it declares one, and the config holds what the kind needs."""
        return self.declared(pipeline) and (self.needs is None or self.needs(config))

    def summary(self, pipeline: PipelineConfig) -> str:
        """The word with its names, as the root help lists it."""
        names = self.names(pipeline) if self.names else list(getattr(pipeline, self.collection))
        return f'{self.word} ({", ".join(names)})' if names else self.word


def managed_definition(job: GlueJobConfig) -> dict[str, Any]:
    """The optional definition fields this job's config decides, and what it sets them to.

    Only the ones it names. An absent key means unmanaged, so the live value survives a deploy —
    reporting it as a null would say dectl writes a null, which is the opposite of what absence
    means and is the one thing a reader of this block must not conclude.

    Built from the model's own declarations, so a field added to either map appears here without
    anything being remembered. The question the whole managed-field surface exists to make
    answerable is which fields are dectl's and which stay Terraform's, and a deploy's diff is a
    write rather than a read."""
    declared = {**job.DEFINITION_FIELDS, **job.NESTED_DEFINITION_FIELDS}
    return {field: getattr(job, field) for field in declared if getattr(job, field) is not None}


def script_uris(job: GlueJobConfig) -> list[str]:
    """Where every script of one job lands, for a reader rather than for a deploy.

    Three operands substituted, exactly as every neighboring row in both renderers substitutes
    the name beside it. Both renderers then show the destination the deploy writes, which is the
    point: two of the faults `config validate` reports are about this composed value and no
    command rendered it.

    `render_env_model` is deliberately not what does the substituting. It also asks the env
    guard, and a renderer that asks it once per glue job answers a question its caller already
    answered for the whole pipeline — with a narrower population, so a hardcoded job beside a
    `{env}` bucket warned that `--env` changed nothing directly above two names it changed."""
    bucket = substitute_env(job.script_bucket)
    prefix = substitute_env(job.script_prefix)
    return [join_uri(bucket, prefix, substitute_env(script)) for script in job.scripts]


def glue_json(pipeline: PipelineConfig, paths: AliasPaths) -> dict[str, Any]:
    return {
        alias: {
            'name': substitute_env(job.name),
            'script_bucket': substitute_env(job.script_bucket),
            'script_prefix': substitute_env(job.script_prefix),
            'scripts': [substitute_env(script) for script in job.scripts],
            # The composed destination, beside the two strings it is built from. Two of the
            # faults `config validate` reports are about this value, and a reader refused
            # over one otherwise has to join two fields by hand to see what was refused.
            'script_uris': script_uris(job),
            # Which definition fields this config decides, so the split between dectl's and
            # Terraform's is answerable from a read rather than from a deploy's diff.
            'managed': managed_definition(job),
            'paths': paths.get(alias, {}),
        }
        for alias, job in pipeline.glue_jobs.items()
    }


def glue_rows(pipeline: PipelineConfig) -> list[ViewRow]:
    rows = []
    for alias, job in pipeline.glue_jobs.items():
        details = [f'deploys to: {uri}' for uri in script_uris(job)]
        managed = managed_definition(job)
        if managed:
            details.append(f'manages: {", ".join(f"{field}={value}" for field, value in managed.items())}')
        rows.append(ViewRow(alias, substitute_env(job.name), details))
    return rows


def lambda_json(pipeline: PipelineConfig, paths: AliasPaths) -> dict[str, Any]:
    return {
        alias: {
            'name': substitute_env(fn.name),
            'durable': fn.durable,
            'source_dir': substitute_env(fn.source_dir),
            'paths': paths.get(alias, {}),
        }
        for alias, fn in pipeline.lambdas.items()
    }


def lambda_rows(pipeline: PipelineConfig) -> list[ViewRow]:
    # Worth calling out inline: a durable function carries a different set of verbs.
    return [ViewRow(alias, f'{substitute_env(fn.name)}{" (durable)" if fn.durable else ""}', []) for alias, fn in pipeline.lambdas.items()]


def sfn_json(pipeline: PipelineConfig, paths: AliasPaths) -> dict[str, Any]:
    return {
        alias: {
            'name': substitute_env(sfn.name),
            'log_group': substitute_env(sfn.log_group),
            'definition_file': substitute_env(sfn.definition_file) if sfn.definition_file is not None else None,
            # What a deploy writes into the definition under this environment, which is the
            # thing to check before deploying to a new one.
            'definition_substitutions': {name: substitute_env(value) for name, value in sfn.definition_substitutions.items()},
            'live_alias': substitute_env(sfn.live_alias) if sfn.live_alias else None,
            'paths': paths.get(alias, {}),
        }
        for alias, sfn in pipeline.step_functions.items()
    }


def sfn_rows(pipeline: PipelineConfig) -> list[ViewRow]:
    rows = []
    for alias, sfn in pipeline.step_functions.items():
        details = [f'${{{name}}}: {substitute_env(value)}' for name, value in sfn.definition_substitutions.items()]
        if sfn.live_alias:
            details.append(f'live alias: {substitute_env(sfn.live_alias)}')
        rows.append(ViewRow(alias, substitute_env(sfn.name), details))
    return rows


def s3_json(pipeline: PipelineConfig, _paths: AliasPaths) -> dict[str, Any]:
    # A bucket names no local file, so its entries carry no `paths`.
    return {alias: {'bucket': substitute_env(bucket)} for alias, bucket in pipeline.buckets.items()}


def s3_rows(pipeline: PipelineConfig) -> list[ViewRow]:
    return [ViewRow(alias, substitute_env(bucket), []) for alias, bucket in pipeline.buckets.items()]


def iceberg_json(pipeline: PipelineConfig, paths: AliasPaths) -> dict[str, Any]:
    return {
        alias: {
            'database': substitute_env(table.database),
            'table': substitute_env(table.table),
            'paths': paths.get(alias, {}),
        }
        for alias, table in pipeline.iceberg_tables.items()
    }


def iceberg_rows(pipeline: PipelineConfig) -> list[ViewRow]:
    return [
        ViewRow(alias, f'{substitute_env(table.database)}.{substitute_env(table.table)}', [])
        for alias, table in pipeline.iceberg_tables.items()
    ]


def jenkins_json(pipeline: PipelineConfig, _paths: AliasPaths) -> dict[str, Any] | None:
    # The block decides whether a `jenkins` command exists and which verbs it has, so it is shown
    # whether or not `config.yaml` names a server.
    job = pipeline.jenkins
    return None if job is None else {'job_path': substitute_env(job.job_path), 'actions': list(job.actions)}


def jenkins_rows(pipeline: PipelineConfig) -> list[ViewRow]:
    job = pipeline.jenkins
    if job is None:
        return []
    actions = ', '.join(job.actions) or 'none, so nothing queues a build'
    return [ViewRow('', f'{substitute_env(job.job_path)} (actions: {actions})', [])]


def jenkins_actions(pipeline: PipelineConfig) -> list[str]:
    return list(pipeline.jenkins.actions) if pipeline.jenkins else []


def jenkins_server_named(config: DectlConfig) -> bool:
    return config.jenkins is not None


# In display order: the root summary, `reference`, `resource_types` and `list` follow it.
RESOURCE_KINDS = (
    ResourceKind(GlueJobConfig.RESOURCE, 'glue_jobs', make_glue_app, glue_json, glue_rows),
    ResourceKind(LambdaConfig.RESOURCE, 'lambdas', make_lambda_app, lambda_json, lambda_rows),
    ResourceKind(StepFunctionConfig.RESOURCE, 'step_functions', make_sfn_app, sfn_json, sfn_rows),
    ResourceKind(PipelineConfig.site_resource('buckets'), 'buckets', make_s3_app, s3_json, s3_rows),
    ResourceKind(IcebergTableConfig.RESOURCE, 'iceberg_tables', make_iceberg_app, iceberg_json, iceberg_rows),
    ResourceKind(
        JenkinsJobConfig.RESOURCE,
        'jenkins',
        make_jenkins_app,
        jenkins_json,
        jenkins_rows,
        panel='Pipeline',
        names=jenkins_actions,
        needs=jenkins_server_named,
    ),
)


def attached_kinds(pipeline: PipelineConfig, config: DectlConfig) -> list[ResourceKind]:
    """The kinds this pipeline has a command for, in display order."""
    return [kind for kind in RESOURCE_KINDS if kind.attached(pipeline, config)]
