import time
from pathlib import Path
from typing import Annotated

import boto3
import typer
from rich.table import Table

from dectl.config import DectlConfig
from dectl.config import GlueJobConfig
from dectl.config import PipelineConfig
from dectl.config import pipeline_path_faults
from dectl.config import resolve_from_root
from dectl.env import render_env_model
from dectl.env import substitute_env
from dectl.logs import tail_glue_run
from dectl.output import console
from dectl.output import emit_json
from dectl.output import error
from dectl.output import info
from dectl.output import success
from dectl.paths import refuse_unusable_paths
from dectl.prompt import confirm_or_exit

RUN_STATE_COLORS = {'SUCCEEDED': 'green', 'FAILED': 'red', 'STOPPED': 'red', 'TIMEOUT': 'red', 'RUNNING': 'cyan'}

TERMINAL_RUN_STATES = frozenset({'SUCCEEDED', 'FAILED', 'STOPPED', 'TIMEOUT', 'ERROR'})
FAILED_RUN_STATES = TERMINAL_RUN_STATES - {'SUCCEEDED'}

# Read-only outputs of GetJob that UpdateJob rejects. AllocatedCapacity is a deprecated
# server-derived mirror of MaxCapacity that conflicts with WorkerType if echoed back.
READ_ONLY_KEYS = ('Name', 'CreatedOn', 'LastModifiedOn', 'ProfileName', 'AllocatedCapacity')


def join_uri(bucket: str, prefix: str, script: str) -> str:
    """The S3 URI of one script, from the three strings it is built out of.

    The one place the shape is written. `script_uri` passes substituted operands and the
    per-alias help panel passes raw ones, so the two cannot spell the destination differently —
    and a change to the shape reaches both."""
    return f's3://{bucket}/{prefix}/{script}'


def script_key(glue_job: GlueJobConfig, script: str) -> str:
    """The S3 key one script is uploaded to, and named by in the job definition.

    The one place this is built. The upload, `ScriptLocation` and `--extra-py-files` all read it,
    and any two of them disagreeing means Glue fetches an object nothing wrote. Both halves are
    substituted, because a `{env}` surviving into a key names an object that cannot exist."""
    return f'{substitute_env(glue_job.script_prefix)}/{substitute_env(script)}'


def script_uri(glue_job: GlueJobConfig, script: str) -> str:
    return join_uri(substitute_env(glue_job.script_bucket), substitute_env(glue_job.script_prefix), substitute_env(script))


def configured_uris(glue_job: GlueJobConfig) -> str:
    """Where this job's scripts are destined, spelled exactly as the config spells them.

    `{env}` is deliberately left standing. This feeds the per-alias help panel, which is a
    `help=` argument evaluated while the command tree is built — before `--env` is parsed — so
    substituting would print the default environment's name under `--env prod`, which is a
    confident wrong answer where the template is an obviously unresolved one."""
    return ' · '.join(join_uri(glue_job.script_bucket, glue_job.script_prefix, script) for script in glue_job.scripts)


def resolve_scripts(pipeline_name: str, pipeline: PipelineConfig, alias: str) -> list[tuple[str, Path]]:
    """Every script of one job as (config string, file on disk), or exit naming what is wrong.

    Run before anything is uploaded and before the definition diff, so `deploy` and `deploy
    --plan` refuse the same configs. A `--plan` that could not see a missing script would report
    a clean definition on a config whose real deploy exits, which is the one question `--plan` is
    run to answer.

    The faults come from `pipeline_path_faults`, scoped by argument, so this door and `config
    validate` report one failure the same way rather than each describing it its own. Scoping
    by argument rather than by filtering the result is what keeps the root fault: a checkout
    that is not there explains every missing script under it, and ten lines naming ten files
    name the cause in none of them."""
    refuse_unusable_paths(pipeline_path_faults(pipeline_name, pipeline, on_disk=True, resource='glue', alias=alias))
    job = pipeline.glue_jobs[alias]
    return [(script, resolve_from_root(pipeline, script)) for script in job.scripts]


def upload_scripts(session: boto3.Session, glue_job: GlueJobConfig, sources: list[tuple[str, Path]]) -> None:
    """Upload each resolved script under the key its config string spells.

    Every path is checked before the first byte goes anywhere: a partial upload leaves the job's
    scripts split across two revisions, and the run that follows mixes them. The caller normally
    checked them too, and repeating it here is what keeps that promise a property of this
    function rather than of whoever calls it.

    The set uploaded has to be the set the definition names. Taking `sources` from the caller
    moved that from a property held by construction — this function used to iterate
    `glue_job.scripts` itself — to one the caller can break: a set omitting `scripts[0]` whose
    every path exists uploads cleanly while `build_job_update` emits a `ScriptLocation` nothing
    wrote, which is exactly what `script_key` exists to prevent."""
    named = {script for script, _ in sources}
    if named != set(glue_job.scripts):
        error(f'{glue_job.name}: asked to upload {sorted(named)}, but the job definition names {sorted(glue_job.scripts)}')
        raise typer.Exit(1)

    missing = [path for _, path in sources if not path.is_file()]
    if missing:
        for path in missing:
            error(f'script not found: {path}')
        raise typer.Exit(1)

    s3 = session.client('s3')
    bucket = substitute_env(glue_job.script_bucket)
    for script, path in sources:
        key = script_key(glue_job, script)
        s3.upload_file(Filename=str(path), Bucket=bucket, Key=key)
        success(f'uploaded {path} -> s3://{bucket}/{key}')


def build_job_update(existing: dict, glue_job: GlueJobConfig) -> dict:
    """Reconstruct the full job definition with dectl's managed fields applied.

    UpdateJob replaces the whole definition rather than patching it, so this starts from the
    existing definition and overrides only what dectl manages. Otherwise every deploy silently
    resets omitted fields (Timeout, GlueVersion, WorkerType, MaxRetries, ...) to their defaults."""
    job_update = {key: value for key, value in existing.items() if key not in READ_ONLY_KEYS}

    # Spark jobs (glueetl) report a derived MaxCapacity alongside WorkerType/NumberOfWorkers, but
    # UpdateJob rejects setting both. Drop MaxCapacity when the job uses the worker-based model.
    worker_based = 'WorkerType' in job_update or 'NumberOfWorkers' in job_update
    if worker_based:
        job_update.pop('MaxCapacity', None)

    if glue_job.max_capacity is not None:
        if worker_based:
            error(f'{glue_job.name} sizes by WorkerType/NumberOfWorkers; remove max_capacity from its config')
            raise typer.Exit(1)
        job_update['MaxCapacity'] = glue_job.max_capacity

    job_update['Role'] = glue_job.role

    command = job_update.get('Command', {})
    command['ScriptLocation'] = script_uri(glue_job, glue_job.scripts[0])
    job_update['Command'] = command

    if glue_job.connections is None:
        connections = list(existing.get('Connections', {}).get('Connections', []))
    else:
        connections = list(glue_job.connections)
    # Glue rejects UpdateJob when Connections is present but its list is empty, so only
    # include it when the job actually has connections.
    if connections:
        job_update['Connections'] = {'Connections': connections}
    else:
        job_update.pop('Connections', None)

    # Merge onto the existing default arguments so args set outside dectl (--TempDir,
    # --additional-python-modules, ...) survive the deploy instead of being wiped.
    default_args = dict(existing.get('DefaultArguments', {}))
    default_args['--JOB_NAME'] = glue_job.name
    if len(glue_job.scripts) > 1:
        default_args['--extra-py-files'] = ','.join(script_uri(glue_job, s) for s in glue_job.scripts[1:])
    for key, value in glue_job.arguments.items():
        arg_key = key if key.startswith('--') else f'--{key}'
        default_args[arg_key] = value
    job_update['DefaultArguments'] = default_args

    return job_update


def render_value(value) -> str:
    return '(unset)' if value is None else str(value)


def diff_mappings(existing: dict, updated: dict) -> dict:
    keys = sorted(set(existing) | set(updated))
    return {key: (existing.get(key), updated.get(key)) for key in keys if existing.get(key) != updated.get(key)}


def job_definition_changes(existing: dict, job_update: dict) -> list[tuple[str, str, str]]:
    """Field-level diff of what UpdateJob would change, for review before it is applied.

    Nested dicts (Command, DefaultArguments, Connections) are expanded one level: whole-dict
    before/after blobs are unreadable, and the interesting change is almost always a single key."""
    changes = []
    for key, new_value in job_update.items():
        old_value = existing.get(key)
        if old_value == new_value:
            continue
        if isinstance(new_value, dict) and (old_value is None or isinstance(old_value, dict)):
            for sub_key, (old_sub, new_sub) in diff_mappings(old_value or {}, new_value).items():
                changes.append((f'{key}.{sub_key}', render_value(old_sub), render_value(new_sub)))
        else:
            changes.append((key, render_value(old_value), render_value(new_value)))

    # Keys dectl drops (a detached connection, MaxCapacity on a Spark job) are absent from
    # job_update, so the loop above cannot see them — removals matter as much as changes here.
    for key, old_value in existing.items():
        if key not in job_update and key not in READ_ONLY_KEYS:
            changes.append((key, render_value(old_value), '(removed)'))

    return sorted(changes)


def render_job_changes(job_name: str, changes: list[tuple[str, str, str]]) -> None:
    table = Table(title=f'{job_name} job definition changes')
    table.add_column('field')
    table.add_column('current', style='red')
    table.add_column('after deploy', style='green')
    for field, old_value, new_value in changes:
        table.add_row(field, old_value, new_value)
    console.print(table)


def plan_glue_job_update(session: boto3.Session, glue_job: GlueJobConfig, assume_yes: bool = False, plan: bool = False) -> dict | None:
    """Everything about a definition update that can refuse: build it, show it, confirm it.

    Terraform owns these jobs once a pipeline is established, so an UpdateJob that silently
    rewrites Role/Connections/DefaultArguments is how dectl's config drifts from the real
    definition. When nothing dectl manages differs, None comes back and the caller skips the
    call entirely — the steady state is then a pure code push with no drift surface at all.

    Everything that can refuse lives here rather than beside the apply, so a caller gets the
    refusals out of the way before it writes anything. `build_job_update` exits on a
    max_capacity Glue would reject, and the confirmation can be declined; either landing after a
    script upload leaves ScriptLocation pointing at code the user was then told not to deploy."""
    glue = session.client('glue')
    existing = glue.get_job(JobName=glue_job.name)['Job']
    job_update = build_job_update(existing, glue_job)

    changes = job_definition_changes(existing, job_update)
    if not changes:
        info(f'{glue_job.name}: job definition unchanged')
        return None

    render_job_changes(glue_job.name, changes)

    if plan:
        return None
    if not assume_yes:
        confirm_or_exit('apply these job definition changes?')
    return job_update


def apply_glue_job_update(session: boto3.Session, glue_job: GlueJobConfig, job_update: dict) -> None:
    session.client('glue').update_job(JobName=glue_job.name, JobUpdate=job_update)
    success(f'updated job {glue_job.name}')


# How far before a run's recorded start to begin scanning for its log events. Glue's StartedOn
# comes from the control plane while the events are stamped by the worker, so they are close but
# not ordered; a few minutes of slack costs nothing against a window this narrow.
LOG_START_SLACK_MS = 5 * 60 * 1000


class GlueRunWatcher:
    """Polls one run's state so a tail knows when to stop, and remembers the state it stopped on.

    The tailer cannot ask Glue anything itself — it only holds a logs client — so it takes this
    as a predicate. Keeping the last observed state here is what lets the caller exit non-zero on
    a failed run without a second get_job_run after the fact."""

    def __init__(self, glue_client, job_name: str, run_id: str) -> None:
        self.glue_client = glue_client
        self.job_name = job_name
        self.run_id = run_id
        self.state = ''

    def finished(self) -> bool:
        self.state = self.glue_client.get_job_run(JobName=self.job_name, RunId=self.run_id)['JobRun']['JobRunState']
        return self.state in TERMINAL_RUN_STATES


def run_log_start(glue_client, job_name: str, run_id: str) -> int:
    """Epoch-ms lower bound for a run's log events, from the run's own StartedOn.

    Without this the tailer scans a group shared by every Glue job in the account from the start
    of its retention, which is minutes of paging before anything prints. A run that has not
    started yet has no StartedOn, so fall back to now — it has certainly written nothing earlier."""
    run = glue_client.get_job_run(JobName=job_name, RunId=run_id)['JobRun']
    started = run.get('StartedOn')
    epoch_ms = int(started.timestamp() * 1000) if started is not None else int(time.time() * 1000)
    return epoch_ms - LOG_START_SLACK_MS


def follow_glue_run(session: boto3.Session, glue_job: GlueJobConfig, run_id: str) -> None:
    """Tail a run to completion and exit non-zero if it did not succeed.

    Tailing starts immediately rather than waiting for the run to reach RUNNING: the log groups
    are polled by run-id prefix, so an unstarted run simply returns nothing until it has written
    something, and the first line lands the moment it exists instead of one status poll later."""
    glue = session.client('glue')
    watcher = GlueRunWatcher(glue, glue_job.name, run_id)
    started_at = run_log_start(glue, glue_job.name, run_id)
    tail_glue_run(session.client('logs'), run_id, started_at, follow=True, run_finished=watcher.finished)

    if watcher.state in FAILED_RUN_STATES:
        error(f'run {run_id} finished {watcher.state}')
        raise typer.Exit(1)
    success(f'run {run_id} {watcher.state}')


def start_glue_run(session: boto3.Session, glue_job: GlueJobConfig, follow: bool) -> None:
    """Start a Glue job run. With follow, tail its logs until the run reaches a terminal state;
    otherwise just print the run id and return (the default — streaming is an explicit opt-in)."""
    glue = session.client('glue')
    resp = glue.start_job_run(JobName=glue_job.name)
    run_id = resp['JobRunId']
    info(f'started job run: {run_id}')

    if follow:
        follow_glue_run(session, glue_job, run_id)


def job_run_to_row(run: dict) -> list[str]:
    state = run.get('JobRunState', '')
    color = RUN_STATE_COLORS.get(state, 'white')
    started = run.get('StartedOn')
    completed = run.get('CompletedOn')
    return [
        run.get('Id', ''),
        f'[{color}]{state}[/{color}]',
        started.isoformat() if started else '',
        completed.isoformat() if completed else '',
    ]


def make_glue_job_app(
    pipeline_name: str, alias: str, job_config: GlueJobConfig, pipeline: PipelineConfig, config: DectlConfig
) -> typer.Typer:
    """Build the per-job sub-app: `dectl PIPELINE glue <alias> <verb>`.

    Verbs close over this job's config and resolve {env} at call time (never at import), so the
    active --env is respected. The sub-app help doubles as an info panel for the job.

    The panel is a third renderer of the destination the other two agree on, so it goes through
    `join_uri` like they do. It shows the config as written, `{env}` and all, and names the
    command that resolves it — the panel is built before `--env` is parsed, so a resolved name
    here would be the default environment's under any other."""
    destinations = configured_uris(job_config) or 'no scripts configured'
    job_app = typer.Typer(
        no_args_is_help=True,
        help=(
            f'Glue job [bold]{alias}[/bold] → {job_config.name}\n\n'
            f'{destinations}\n\n'
            f'As configured. Run "dectl {pipeline_name} list" for the active environment.'
        ),
    )

    def resolved() -> GlueJobConfig:
        return render_env_model(job_config)

    @job_app.command(
        epilog=(
            'Examples:\n\n'
            f'dectl {pipeline_name} glue {alias} deploy — upload scripts, confirm any definition changes\n\n'
            f'dectl {pipeline_name} glue {alias} deploy --plan — show what would change, touch nothing\n\n'
            f'dectl {pipeline_name} glue {alias} deploy --yes — no prompt, for the pre-Terraform loop'
        ),
    )
    def deploy(
        plan: Annotated[bool, typer.Option('--plan', help='Show pending job definition changes and exit without applying.')] = False,
        yes: Annotated[bool, typer.Option('--yes', '-y', help='Apply job definition changes without confirming.')] = False,
    ) -> None:
        """Upload the job's scripts to S3 and point the Glue job at them (does not run it).

        Changes to the job definition itself — role, connections, capacity, arguments — are shown
        and confirmed before they are applied, since Terraform owns those once a pipeline is
        established. When nothing differs, the definition is left untouched."""
        from dectl.session import make_session

        job = resolved()
        # Resolve and check before anything reaches AWS, so --plan refuses what deploy refuses.
        sources = resolve_scripts(pipeline_name, pipeline, alias)
        session = make_session(config)
        job_update = plan_glue_job_update(session, job, assume_yes=yes, plan=plan)
        if plan:
            return
        upload_scripts(session, job, sources)
        if job_update is not None:
            apply_glue_job_update(session, job, job_update)

    @job_app.command(epilog=f'Example:\n\ndectl {pipeline_name} glue {alias} run --follow')
    def run(
        follow: Annotated[bool, typer.Option('--follow', '-f', help="Tail the run's logs until it finishes.")] = False,
    ) -> None:
        """Start the Glue job. Prints the run id; add --follow to stream its output and error logs."""
        from dectl.session import make_session

        start_glue_run(make_session(config), resolved(), follow=follow)

    @job_app.command(
        epilog=(
            'Examples:\n\n'
            f'dectl {pipeline_name} glue {alias} logs — show the most recent run\n\n'
            f'dectl {pipeline_name} glue {alias} logs jr_abc123 --follow — tail a specific run'
        ),
    )
    def logs(
        run_id: Annotated[str | None, typer.Argument(help='Run ID to show. Defaults to the most recent run.')] = None,
        follow: Annotated[bool, typer.Option('--follow', '-f', help='Keep tailing while the run is still going.')] = False,
    ) -> None:
        """Show CloudWatch output and error logs for a Glue job run (defaults to the latest run)."""
        from dectl.session import make_session

        job = resolved()
        session = make_session(config)

        if not run_id:
            glue = session.client('glue')
            resp = glue.get_job_runs(JobName=job.name, MaxResults=1)
            recent = resp.get('JobRuns', [])
            if not recent:
                info('no recent job runs found')
                raise typer.Exit(1)
            run_id = recent[0]['Id']
            info(f'using most recent run: {run_id}')

        if follow:
            follow_glue_run(session, job, run_id)
        else:
            started_at = run_log_start(session.client('glue'), job.name, run_id)
            tail_glue_run(session.client('logs'), run_id, started_at, follow=False)

    @job_app.command(epilog=f'Example:\n\ndectl {pipeline_name} glue {alias} runs --limit 5')
    def runs(
        limit: Annotated[int, typer.Option('--limit', '-n', help='Number of runs to show.')] = 10,
        as_json: Annotated[bool, typer.Option('--json', help='Emit machine-readable JSON to stdout.')] = False,
    ) -> None:
        """List recent runs of this Glue job with their state and timing."""
        from dectl.session import make_session

        job = resolved()
        glue = make_session(config).client('glue')
        job_runs = glue.get_job_runs(JobName=job.name, MaxResults=limit).get('JobRuns', [])

        if as_json:
            emit_json(
                [
                    {
                        'id': r.get('Id'),
                        'state': r.get('JobRunState'),
                        'started': r.get('StartedOn'),
                        'completed': r.get('CompletedOn'),
                    }
                    for r in job_runs
                ]
            )
            return

        if not job_runs:
            info('no runs found')
            return
        table = Table(title=f'{alias} runs')
        table.add_column('run id')
        table.add_column('state')
        table.add_column('started')
        table.add_column('completed')
        for r in job_runs:
            table.add_row(*job_run_to_row(r))
        console.print(table)

    return job_app


def make_glue_app(pipeline_name: str, pipeline: PipelineConfig, config: DectlConfig) -> typer.Typer:
    glue_jobs = pipeline.glue_jobs
    alias_list = ', '.join(glue_jobs.keys()) or '(none configured)'

    glue_app = typer.Typer(
        no_args_is_help=True,
        help=f'Glue jobs in [bold]{pipeline_name}[/bold] — pick a job, then a verb.\n\nJobs: {alias_list}',
    )
    for alias, job_config in glue_jobs.items():
        glue_app.add_typer(
            make_glue_job_app(pipeline_name, alias, job_config, pipeline, config),
            name=alias,
            rich_help_panel='Jobs',
        )
    return glue_app
