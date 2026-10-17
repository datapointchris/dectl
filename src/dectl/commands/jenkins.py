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


def make_jenkins_app(pipeline_name: str, job_config: JenkinsJobConfig, config: DectlConfig) -> typer.Typer:
    """Build `dectl PIPELINE jenkins`: one verb per configured action, then `status` and `logs`.

    What a build does is the job's business, so dectl names no action itself. The panel and each
    verb's help show the config as written, because they are built before `--env` is parsed."""
    jenkins_app = typer.Typer(
        help=(
            f'Build the Jenkins job of [bold]{pipeline_name}[/bold]: {job_config.job_path}\n\n'
            'Each action from the config queues a build posting its own parameters. "status" and '
            '"logs" read the last build without starting a new one.'
        ),
        no_args_is_help=True,
    )

    def resolved() -> JenkinsJobConfig:
        # Per call rather than once, because --env is not parsed until the command runs.
        return render_env_model(job_config)

    def queue_build(action: str, follow: bool) -> None:
        job = resolved()
        params = {**job.parameters, **job.actions[action]}
        resp = jenkins_request(config, 'POST', f'{job_url_path(job)}/buildWithParameters', data=params)

        if resp.status_code == 201:
            info(f'build queued for {pipeline_name} ({action})')
        else:
            error(f'failed to queue build: {resp.status_code} {resp.text}')
            raise typer.Exit(1)

        if follow:
            time.sleep(3)
            stream_console(job)

    def add_action_verb(action: str, own_parameters: dict[str, str]) -> None:
        posted = ', '.join(f'{name}={value}' for name, value in own_parameters.items())
        summary = f'posting {posted} on top of its parameters' if posted else 'posting its parameters alone'

        @jenkins_app.command(
            name=action,
            help=f'Queue a build of the job, {summary}.',
            epilog=f'Example:\n\ndectl {pipeline_name} jenkins {action} --follow',
        )
        def queue(
            follow: Annotated[bool, typer.Option('--follow', '-f', help='Stream console output after triggering')] = False,
        ) -> None:
            queue_build(action, follow)

    for action, own_parameters in job_config.actions.items():
        add_action_verb(action, own_parameters)

    @jenkins_app.command()
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

    @jenkins_app.command()
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

    return jenkins_app
