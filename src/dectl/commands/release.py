import os
import re
import time
from typing import Annotated

import httpx2
import typer

from dectl.config import CONFIG_PATH
from dectl.config import DectlConfig
from dectl.config import JenkinsJobConfig
from dectl.env import render_env_model
from dectl.output import emit_json
from dectl.output import error
from dectl.output import info
from dectl.output import success


def resolve_env(value: str) -> str:
    if value.startswith('${') and value.endswith('}'):
        var_name = value[2:-1]
        resolved = os.environ.get(var_name, '')
        if not resolved:
            error(f'environment variable {var_name} is not set')
            raise typer.Exit(1)
        return resolved
    return value


def jenkins_request(config: DectlConfig, method: str, path: str, **kwargs):
    jenkins = config.jenkins
    if not jenkins:
        error(f'no jenkins config in {CONFIG_PATH}')
        raise typer.Exit(1)

    url = f'{jenkins.url.rstrip("/")}/{path.lstrip("/")}'
    auth = (resolve_env(jenkins.user), resolve_env(jenkins.token))

    with httpx2.Client(verify=False, auth=auth, timeout=30) as client:  # nosec B501
        resp = client.request(method, url, **kwargs)
        return resp


def job_url_path(job: JenkinsJobConfig) -> str:
    """The job's URL path on the Jenkins server, from a `folder/job` path."""
    return '/job/' + job.job_path.replace('/', '/job/')


def make_release_app(pipeline_name: str, job_config: JenkinsJobConfig, config: DectlConfig) -> typer.Typer:
    release_app = typer.Typer(
        help=(
            f'Release [bold]{pipeline_name}[/bold] through Jenkins + Terraform (the production path).\n\n'
            '"deploy" queues a release build and "plan" a terraform plan build. "status" and "logs" '
            'inspect the last build without starting a new one.'
        ),
        no_args_is_help=True,
    )

    def resolved() -> JenkinsJobConfig:
        # Per call rather than once, because --env is not parsed until the command runs.
        return render_env_model(job_config)

    def queue_build(action: str, follow: bool) -> None:
        job = resolved()
        params = {**job.parameters, 'ACTION': action}
        resp = jenkins_request(config, 'POST', f'{job_url_path(job)}/buildWithParameters', data=params)

        if resp.status_code == 201:
            info(f'build queued for {pipeline_name} (action={action})')
        else:
            error(f'failed to queue build: {resp.status_code} {resp.text}')
            raise typer.Exit(1)

        if follow:
            time.sleep(3)
            stream_console(job)

    @release_app.command(epilog=f'Example:\n\ndectl {pipeline_name} release deploy --follow')
    def deploy(
        follow: Annotated[bool, typer.Option('--follow', '-f', help='Stream console output after triggering')] = False,
    ) -> None:
        """Queue a Jenkins build that runs terraform apply for this pipeline.

        This is the canonical release path: it publishes the real function versions and moves
        aliases through Terraform. Use "dectl PIPELINE lambda <alias> deploy --publish" for fast
        local iteration between releases."""
        queue_build('deploy', follow)

    @release_app.command(epilog=f'Example:\n\ndectl {pipeline_name} release plan --follow')
    def plan(
        follow: Annotated[bool, typer.Option('--follow', '-f', help='Stream console output after triggering')] = False,
    ) -> None:
        """Queue a Jenkins build that runs terraform plan only, applying nothing.

        The plan runs on Jenkins, so this queues a build like deploy does, and that build is the
        one status and logs then report."""
        queue_build('dry_run', follow)

    @release_app.command()
    def status(
        as_json: Annotated[bool, typer.Option('--json', help='Emit machine-readable JSON to stdout.')] = False,
    ) -> None:
        """Show the result of the most recent Jenkins build for this pipeline."""
        resp = jenkins_request(config, 'GET', f'{job_url_path(resolved())}/lastBuild/api/json')

        if resp.status_code != 200:
            error(f'failed to get build status: {resp.status_code}')
            raise typer.Exit(1)

        data = resp.json()
        building = data.get('building', False)
        result = data.get('result', 'IN PROGRESS' if building else 'UNKNOWN')
        number = data.get('number', '?')
        display = data.get('displayName', f'#{number}')

        if as_json:
            emit_json({'build': display, 'number': number, 'building': building, 'result': result})
            return

        if building:
            info(f'{display} - [yellow]BUILDING[/yellow]')
        elif result == 'SUCCESS':
            success(f'{display} - {result}')
        else:
            error(f'{display} - {result}')

    @release_app.command()
    def logs(
        follow: Annotated[bool, typer.Option('--follow', '-f', help='Keep tailing while the build is still running')] = False,
    ) -> None:
        """Show (and optionally tail) console output from the most recent build."""
        stream_console(resolved(), follow=follow)

    def strip_jenkins_noise(text: str) -> str:
        text = re.sub(r'\x1b\[8m.*?\x1b\[0m', '', text)
        lines = text.splitlines(keepends=True)
        return ''.join(line for line in lines if not line.strip().startswith('[Pipeline]'))

    def stream_console(job: JenkinsJobConfig, follow: bool = True) -> None:
        offset = 0

        while True:
            resp = jenkins_request(config, 'GET', f'{job_url_path(job)}/lastBuild/logText/progressiveText?start={offset}')

            if resp.status_code != 200:
                error(f'failed to get logs: {resp.status_code}')
                raise typer.Exit(1)

            text = resp.text
            if text:
                print(strip_jenkins_noise(text), end='')

            new_offset = int(resp.headers.get('X-Text-Size', offset))
            more_data = resp.headers.get('X-More-Data', 'false').lower() == 'true'

            if not more_data or not follow:
                if not follow and more_data:
                    info('\n[yellow](build still running — use --follow to tail)[/yellow]')
                break

            offset = new_offset
            time.sleep(2)

    return release_app
