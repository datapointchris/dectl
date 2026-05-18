import time
from typing import Annotated

import boto3
import typer

from dectl.config import DectlConfig
from dectl.config import GlueJobConfig
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

    command = existing.get('Command', {})
    script_key = f'{glue_job.script_prefix}/{glue_job.scripts[0]}'
    command['ScriptLocation'] = f's3://{glue_job.script_bucket}/{script_key}'

    default_args = {'--JOB_NAME': glue_job.name}
    if len(glue_job.scripts) > 1:
        extra_files = ','.join(f's3://{glue_job.script_bucket}/{glue_job.script_prefix}/{s}' for s in glue_job.scripts[1:])
        default_args['--extra-py-files'] = extra_files
    for key, value in glue_job.arguments.items():
        arg_key = key if key.startswith('--') else f'--{key}'
        default_args[arg_key] = value

    glue.update_job(
        JobName=glue_job.name,
        JobUpdate={
            'Role': glue_job.role,
            'Command': command,
            'DefaultArguments': default_args,
        },
    )
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
    glue_app = typer.Typer(help=f'Glue pipeline: {pipeline_name}')
    glue_jobs = pipeline.glue_jobs

    def resolve_job(job_name: str) -> GlueJobConfig:
        if job_name not in glue_jobs:
            known = ', '.join(glue_jobs.keys())
            error(f'unknown job "{job_name}" for pipeline {pipeline_name}. known: {known}')
            raise typer.Exit(1)
        return glue_jobs[job_name]

    @glue_app.command()
    def deploy(
        job: Annotated[str, typer.Argument(help='Glue job alias from config')],
    ) -> None:
        """Upload scripts to S3 and update the Glue job."""
        from dectl.session import make_session

        glue_job = resolve_job(job)
        session = make_session(config)
        upload_scripts(session, glue_job)
        update_glue_job(session, glue_job)

    @glue_app.command()
    def run(
        job: Annotated[str, typer.Argument(help='Glue job alias from config')],
    ) -> None:
        """Start the Glue job and tail logs."""
        from dectl.session import make_session

        glue_job = resolve_job(job)
        session = make_session(config)
        start_and_tail(session, glue_job)

    @glue_app.command()
    def logs(
        job: Annotated[str, typer.Argument(help='Glue job alias from config')],
        run_id: Annotated[str | None, typer.Argument(help='Glue job run ID to tail')] = None,
    ) -> None:
        """Tail CloudWatch logs for a Glue job run."""
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
