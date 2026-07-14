import time
from typing import Annotated

import boto3
import typer

from dectl.config import DectlConfig
from dectl.config import GlueJobConfig
from dectl.env import render_env_model
from dectl.logs import tail_glue_run
from dectl.output import error
from dectl.output import info
from dectl.output import success


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


def start_and_tail(session: boto3.Session, glue_job: GlueJobConfig) -> None:
    glue = session.client('glue')
    logs_client = session.client('logs')

    resp = glue.start_job_run(JobName=glue_job.name)
    run_id = resp['JobRunId']
    info(f'started job run: {run_id}')

    info('waiting for job to start...')
    while True:
        run = glue.get_job_run(JobName=glue_job.name, RunId=run_id)
        status = run['JobRun']['JobRunState']
        info(f'status: {status}')
        if status in ('RUNNING', 'SUCCEEDED', 'FAILED', 'STOPPED'):
            break
        time.sleep(5)

    tail_glue_run(logs_client, run_id, follow=True)


def make_glue_app(pipeline_name: str, pipeline, config: DectlConfig) -> typer.Typer:
    glue_jobs = pipeline.glue_jobs
    alias_list = ', '.join(glue_jobs.keys()) or '(none configured)'
    example = next(iter(glue_jobs), 'JOB')
    job_arg_help = f'Glue job alias from config. Available: {alias_list}'

    glue_app = typer.Typer(
        no_args_is_help=True,
        help=(f'Deploy scripts, run, and tail Glue jobs in [bold]{pipeline_name}[/bold].\n\nJobs: {alias_list}'),
    )

    def resolve_job(job_name: str) -> GlueJobConfig:
        if job_name not in glue_jobs:
            known = ', '.join(glue_jobs.keys())
            error(f'unknown job "{job_name}" for pipeline {pipeline_name}. known: {known}')
            raise typer.Exit(1)
        return render_env_model(glue_jobs[job_name])

    @glue_app.command(
        epilog=f'Example:\n\ndectl {pipeline_name} glue deploy {example}',
    )
    def deploy(
        job: Annotated[str, typer.Argument(help=job_arg_help)],
    ) -> None:
        """Upload the job's scripts to S3 and point the Glue job at them.

        Does not start the job — run it separately with the [bold]run[/bold] command.
        """
        from dectl.session import make_session

        glue_job = resolve_job(job)
        session = make_session(config)
        upload_scripts(session, glue_job)
        update_glue_job(session, glue_job)

    @glue_app.command(
        epilog=f'Example:\n\ndectl {pipeline_name} glue run {example}',
    )
    def run(
        job: Annotated[str, typer.Argument(help=job_arg_help)],
    ) -> None:
        """Start the Glue job and stream its output and error logs until it finishes."""
        from dectl.session import make_session

        glue_job = resolve_job(job)
        session = make_session(config)
        start_and_tail(session, glue_job)

    @glue_app.command(
        epilog=(
            'Examples:\n\n'
            f'dectl {pipeline_name} glue logs {example} — tail the most recent run\n\n'
            f'dectl {pipeline_name} glue logs {example} jr_abc123 — tail a specific run'
        ),
    )
    def logs(
        job: Annotated[str, typer.Argument(help=job_arg_help)],
        run_id: Annotated[str | None, typer.Argument(help='Run ID to tail. Defaults to the most recent run.')] = None,
    ) -> None:
        """Tail CloudWatch output and error logs for a Glue job run (defaults to the latest run)."""
        from dectl.session import make_session

        glue_job = resolve_job(job)
        session = make_session(config)

        if not run_id:
            glue = session.client('glue')
            resp = glue.get_job_runs(JobName=glue_job.name, MaxResults=1)
            runs = resp.get('JobRuns', [])
            if not runs:
                error('no recent job runs found')
                raise typer.Exit(1)
            run_id = runs[0]['Id']
            info(f'using most recent run: {run_id}')

        logs_client = session.client('logs')
        tail_glue_run(logs_client, run_id, follow=True)

    return glue_app
