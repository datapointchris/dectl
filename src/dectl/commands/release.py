import os
import re
import time
from typing import Annotated

import typer

from dectl.config import DectlConfig
from dectl.config import JenkinsJobConfig
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
    import httpx

    jenkins = config.jenkins
    if not jenkins:
        error('no jenkins config in ~/.config/dectl/config.yaml')
        raise typer.Exit(1)

    url = f'{jenkins.url.rstrip("/")}/{path.lstrip("/")}'
    auth = (resolve_env(jenkins.user), resolve_env(jenkins.token))

    with httpx.Client(verify=False, auth=auth, timeout=30) as client:  # nosec B501
        resp = client.request(method, url, **kwargs)
        return resp


def make_release_app(pipeline_name: str, job_config: JenkinsJobConfig, config: DectlConfig) -> typer.Typer:
    release_app = typer.Typer(
        help=(
            f'Release [bold]{pipeline_name}[/bold] through Jenkins + Terraform (the production path).\n\n'
            'Running this with no subcommand triggers a release build. Use "status" and "logs" '
            'to inspect the last build without starting a new one.'
        ),
    )

    @release_app.callback(
        invoke_without_command=True,
        epilog=(
            'Examples:\n\n'
            f'dectl {pipeline_name} release — trigger a release and stream the console\n\n'
            f'dectl {pipeline_name} release --plan — run terraform plan only\n\n'
            f'dectl {pipeline_name} release status — show the last build result'
        ),
    )
    def release(
        ctx: typer.Context,
        plan: Annotated[bool, typer.Option('--plan', help='Run terraform plan only, do not apply')] = False,
        follow: Annotated[bool, typer.Option('--follow', '-f', help='Stream console output after triggering')] = False,
    ) -> None:
        """Trigger a Jenkins Terraform release build for this pipeline.

        This is the canonical release path: it publishes the real function
        versions and moves aliases through Terraform. Use "dectl PIPELINE lambda
        <alias> deploy --publish" for fast local iteration between releases.
        """
        if ctx.invoked_subcommand is not None:
            return

        params = dict(job_config.parameters)
        params['ACTION'] = 'dry_run' if plan else 'deploy'

        job_path = job_config.job_path.replace('/', '/job/')
        resp = jenkins_request(config, 'POST', f'/job/{job_path}/buildWithParameters', data=params)

        if resp.status_code == 201:
            info(f'build queued for {pipeline_name} (action={params["ACTION"]})')
        else:
            error(f'failed to queue build: {resp.status_code} {resp.text}')
            raise typer.Exit(1)

        if follow:
            time.sleep(3)
            stream_console(config, job_config)

    @release_app.command()
    def status(
        as_json: Annotated[bool, typer.Option('--json', help='Emit machine-readable JSON to stdout.')] = False,
    ) -> None:
        """Show the result of the most recent Jenkins build for this pipeline."""
        job_path = job_config.job_path.replace('/', '/job/')
        resp = jenkins_request(config, 'GET', f'/job/{job_path}/lastBuild/api/json')

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
        stream_console(config, job_config, follow=follow)

    def strip_jenkins_noise(text: str) -> str:
        text = re.sub(r'\x1b\[8m.*?\x1b\[0m', '', text)
        lines = text.splitlines(keepends=True)
        return ''.join(line for line in lines if not line.strip().startswith('[Pipeline]'))

    def stream_console(cfg: DectlConfig, jc: JenkinsJobConfig, follow: bool = True) -> None:
        job_path = jc.job_path.replace('/', '/job/')
        offset = 0

        while True:
            resp = jenkins_request(cfg, 'GET', f'/job/{job_path}/lastBuild/logText/progressiveText?start={offset}')

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
