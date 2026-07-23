import time
from typing import Annotated

import boto3
import typer
from rich.table import Table

from dectl.config import DectlConfig
from dectl.config import GlueJobConfig
from dectl.env import render_env_model
from dectl.logs import tail_glue_run
from dectl.output import console
from dectl.output import emit_json
from dectl.output import info
from dectl.output import success

RUN_STATE_COLORS = {'SUCCEEDED': 'green', 'FAILED': 'red', 'STOPPED': 'red', 'TIMEOUT': 'red', 'RUNNING': 'cyan'}


def upload_scripts(session: boto3.Session, glue_job: GlueJobConfig) -> None:
    s3 = session.client('s3')
    for script in glue_job.scripts:
        key = f'{glue_job.script_prefix}/{script}'
        s3.upload_file(Filename=script, Bucket=glue_job.script_bucket, Key=key)
        success(f'uploaded {script} -> s3://{glue_job.script_bucket}/{key}')


def update_glue_job(session: boto3.Session, glue_job: GlueJobConfig) -> None:
    glue = session.client('glue')
    resp = glue.get_job(JobName=glue_job.name)
    existing = resp['Job']

    # UpdateJob replaces the whole job definition rather than patching it, so start from
    # the existing definition and override only what dectl manages. Otherwise every deploy
    # silently resets omitted fields (Timeout, GlueVersion, WorkerType, MaxRetries, ...) to
    # their defaults. Name/CreatedOn/LastModifiedOn/ProfileName are read-only and rejected by
    # UpdateJob; AllocatedCapacity is a deprecated server-derived mirror of MaxCapacity that
    # conflicts with WorkerType if echoed back, so drop all five.
    read_only_keys = ('Name', 'CreatedOn', 'LastModifiedOn', 'ProfileName', 'AllocatedCapacity')
    job_update = {key: value for key, value in existing.items() if key not in read_only_keys}

    # Spark jobs (glueetl) report a derived MaxCapacity alongside WorkerType/NumberOfWorkers, but
    # UpdateJob rejects setting both. Drop MaxCapacity when the job uses the worker-based model.
    if 'WorkerType' in job_update or 'NumberOfWorkers' in job_update:
        job_update.pop('MaxCapacity', None)

    job_update['Role'] = glue_job.role

    command = job_update.get('Command', {})
    script_key = f'{glue_job.script_prefix}/{glue_job.scripts[0]}'
    command['ScriptLocation'] = f's3://{glue_job.script_bucket}/{script_key}'
    job_update['Command'] = command

    connections = list(existing.get('Connections', {}).get('Connections', []))
    for connection in glue_job.connections:
        if connection not in connections:
            connections.append(connection)
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
        extra_files = ','.join(f's3://{glue_job.script_bucket}/{glue_job.script_prefix}/{s}' for s in glue_job.scripts[1:])
        default_args['--extra-py-files'] = extra_files
    for key, value in glue_job.arguments.items():
        arg_key = key if key.startswith('--') else f'--{key}'
        default_args[arg_key] = value
    job_update['DefaultArguments'] = default_args

    glue.update_job(JobName=glue_job.name, JobUpdate=job_update)
    success(f'updated job {glue_job.name}')


def start_glue_run(session: boto3.Session, glue_job: GlueJobConfig, follow: bool) -> None:
    """Start a Glue job run. With follow, wait for it to reach RUNNING then tail its logs;
    otherwise just print the run id and return (the default — streaming is an explicit opt-in)."""
    glue = session.client('glue')
    resp = glue.start_job_run(JobName=glue_job.name)
    run_id = resp['JobRunId']
    info(f'started job run: {run_id}')

    if not follow:
        return

    info('waiting for job to start...')
    while True:
        run = glue.get_job_run(JobName=glue_job.name, RunId=run_id)
        status = run['JobRun']['JobRunState']
        info(f'status: {status}')
        if status in ('RUNNING', 'SUCCEEDED', 'FAILED', 'STOPPED'):
            break
        time.sleep(5)

    tail_glue_run(session.client('logs'), run_id, follow=True)


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


def make_glue_job_app(pipeline_name: str, alias: str, job_config: GlueJobConfig, config: DectlConfig) -> typer.Typer:
    """Build the per-job sub-app: `dectl PIPELINE glue <alias> <verb>`.

    Verbs close over this job's config and resolve {env} at call time (never at import), so the
    active --env is respected. The sub-app help doubles as an info panel for the job."""
    job_app = typer.Typer(
        no_args_is_help=True,
        help=(
            f'Glue job [bold]{alias}[/bold] → {job_config.name}\n\n'
            f'Scripts: {", ".join(job_config.scripts)} · bucket: s3://{job_config.script_bucket}/{job_config.script_prefix}/'
        ),
    )

    def resolved() -> GlueJobConfig:
        return render_env_model(job_config)

    @job_app.command(epilog=f'Example:\n\ndectl {pipeline_name} glue {alias} deploy')
    def deploy() -> None:
        """Upload the job's scripts to S3 and point the Glue job at them (does not run it)."""
        from dectl.session import make_session

        job = resolved()
        session = make_session(config)
        upload_scripts(session, job)
        update_glue_job(session, job)

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

        tail_glue_run(session.client('logs'), run_id, follow=follow)

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


def make_glue_app(pipeline_name: str, pipeline, config: DectlConfig) -> typer.Typer:
    glue_jobs = pipeline.glue_jobs
    alias_list = ', '.join(glue_jobs.keys()) or '(none configured)'

    glue_app = typer.Typer(
        no_args_is_help=True,
        help=f'Glue jobs in [bold]{pipeline_name}[/bold] — pick a job, then a verb.\n\nJobs: {alias_list}',
    )
    for alias, job_config in glue_jobs.items():
        glue_app.add_typer(
            make_glue_job_app(pipeline_name, alias, job_config, config),
            name=alias,
            rich_help_panel='Jobs',
        )
    return glue_app
