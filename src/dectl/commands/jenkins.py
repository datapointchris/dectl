import os
import re
import time
from typing import TYPE_CHECKING
from typing import Annotated

import typer

from dectl.config import CONFIG_PATH
from dectl.config import DectlConfig
from dectl.config import JenkinsJobConfig
from dectl.config import PipelineConfig
from dectl.env import render_env_model
from dectl.output import emit_json
from dectl.output import error
from dectl.output import info
from dectl.output import success

if TYPE_CHECKING:
    import httpx2


def resolve_env(value: str) -> str:
    if value.startswith('${') and value.endswith('}'):
        var_name = value[2:-1]
        resolved = os.environ.get(var_name, '')
        if not resolved:
            error(f'environment variable {var_name} is not set')
            raise typer.Exit(1)
        return resolved
    return value


class JenkinsError(Exception):
    """A Jenkins request that failed in transport or answered a status its caller did not expect.

    The root group ends on its message, so no caller checks a status or catches a transport
    error itself, and the next verb gets both by passing the status it expects."""


def jenkins_request(config: DectlConfig, method: str, path: str, expected_status: int, **kwargs) -> 'httpx2.Response':
    # The HTTP client is imported when a request is made, so no command that sends none loads it.
    import httpx2

    jenkins = config.jenkins
    if not jenkins:
        error(f'no jenkins config in {CONFIG_PATH}')
        raise typer.Exit(1)

    url = f'{jenkins.url.rstrip("/")}/{path.lstrip("/")}'
    auth = (resolve_env(jenkins.user), resolve_env(jenkins.token))

    try:
        with httpx2.Client(auth=auth, timeout=30) as client:
            resp = client.request(method, url, **kwargs)
    except httpx2.HTTPError as exc:
        raise JenkinsError(f'Jenkins {method} {url} failed: {exc}') from exc

    if resp.status_code != expected_status:
        detail = f': {resp.text.strip()}' if resp.text.strip() else ''
        raise JenkinsError(f'Jenkins {method} {url} answered {resp.status_code}, expected {expected_status}{detail}')
    return resp


def job_url_path(job: JenkinsJobConfig) -> str:
    """The job's URL path on the Jenkins server, from a `folder/job` path."""
    return '/job/' + job.job_path.replace('/', '/job/')


def build_url_path(job: JenkinsJobConfig, build: int | None) -> str:
    """One build's URL path: the numbered one, or with None the job's most recent."""
    return f'{job_url_path(job)}/{"lastBuild" if build is None else build}'


# Jenkins names the queue item in the 201's Location. Only its number is read, and the request
# goes to the configured URL, because a proxy can make the Location's host differ from it.
QUEUE_ITEM_LOCATION = re.compile(r'/queue/item/(\d+)/?$')
QUEUE_POLL_SECONDS = 2


def queue_item_number(response: 'httpx2.Response') -> int:
    location = response.headers.get('Location', '')
    match = QUEUE_ITEM_LOCATION.search(location)
    if match is None:
        raise JenkinsError(
            f'Jenkins queued the build without naming its queue item (Location: {location or "absent"}), '
            'so dectl cannot tell its build from any other'
        )
    return int(match.group(1))


def wait_for_build_number(config: DectlConfig, item: int) -> dict:
    """Poll one queue item until Jenkins starts its build, and return the build's `executable`.

    A build has no number while it is queued, and `lastBuild` is meanwhile the previous one, so
    the number is the only handle that names the build this POST queued. Each new reason Jenkins
    gives for the wait is printed once, compared without its digits so a countdown is one reason."""
    reported = None
    while True:
        queued = jenkins_request(config, 'GET', f'/queue/item/{item}/api/json', 200).json()
        if queued.get('executable'):
            return queued['executable']
        if queued.get('cancelled'):
            error(f'queue item {item} was cancelled in Jenkins before its build started')
            raise typer.Exit(1)
        why = queued.get('why') or ''
        reason = re.sub(r'[\d.]+', '', why)
        if why and reason != reported:
            reported = reason
            info(f'waiting for Jenkins to start it: {why}')
        time.sleep(QUEUE_POLL_SECONDS)


def make_jenkins_app(pipeline_name: str, pipeline: PipelineConfig, config: DectlConfig) -> typer.Typer:
    """Build `dectl PIPELINE jenkins`: one verb per configured action, then `status` and `logs`.

    What a build does is the job's business, so dectl names no action itself. The panel and each
    verb's help show the config as written, because they are built before `--env` is parsed."""
    job_config = pipeline.jenkins
    if job_config is None:
        raise ValueError(f'{pipeline_name} declares no jenkins job, so it has no jenkins command')
    if job_config.actions:
        verbs = 'Each action from the config queues a build posting its own parameters, and prints its number once it starts.'
    else:
        verbs = (
            'No actions are configured, so nothing here queues a build. Each key under this '
            "pipeline's jenkins.actions becomes a verb, posting its own parameters on top of the "
            'job\'s, as "dectl config example" shows.'
        )
    jenkins_app = typer.Typer(
        help=(
            f'Build the Jenkins job of [bold]{pipeline_name}[/bold]: {job_config.job_path}\n\n'
            f'{verbs} "status" and "logs" read the build whose number they are given, or the most recent one.'
        ),
        no_args_is_help=True,
    )

    def resolved() -> JenkinsJobConfig:
        # Per call rather than once, because --env is not parsed until the command runs.
        return render_env_model(job_config)

    def queue_build(action: str, follow: bool) -> None:
        job = resolved()
        params = {**job.parameters, **job.actions[action]}
        response = jenkins_request(config, 'POST', f'{job_url_path(job)}/buildWithParameters', 201, data=params)
        item = queue_item_number(response)
        info(f'build queued for {pipeline_name} ({action}) as queue item {item}')
        executable = wait_for_build_number(config, item)
        number = int(executable['number'])
        success(f'build #{number} started: {executable.get("url", build_url_path(job, number))}')

        if follow:
            stream_console(job, number)
            result = finished_result(job, number)
            if result != 'SUCCESS':
                error(f'build #{number} - {result}')
                raise typer.Exit(3)

    def finished_result(job: JenkinsJobConfig, number: int) -> str:
        """The result of a build whose console has ended, read once Jenkins marks it finished."""
        while True:
            data = jenkins_request(config, 'GET', f'{build_url_path(job, number)}/api/json', 200).json()
            if not data.get('building', False):
                return data.get('result') or 'UNKNOWN'
            time.sleep(2)

    def add_action_verb(action: str, own_parameters: dict[str, str]) -> None:
        posted = ', '.join(f'{name}={value}' for name, value in own_parameters.items())
        summary = f'posting {posted} on top of its parameters' if posted else 'posting its parameters alone'

        @jenkins_app.command(
            name=action,
            help=f'Queue a build of the job, {summary}.',
            epilog=f'Example:\n\ndectl {pipeline_name} jenkins {action} --follow',
        )
        def queue(
            follow: Annotated[
                bool, typer.Option('--follow', '-f', help='Stream console output after triggering, and exit 3 unless the build succeeds')
            ] = False,
        ) -> None:
            queue_build(action, follow)

    for action, own_parameters in job_config.actions.items():
        add_action_verb(action, own_parameters)

    build_argument = typer.Argument(help='The build number an action printed. Defaults to the most recent build.', metavar='BUILD', min=1)

    @jenkins_app.command(epilog=f'Example:\n\ndectl {pipeline_name} jenkins status 42')
    def status(
        build: Annotated[int | None, build_argument] = None,
        as_json: Annotated[bool, typer.Option('--json', help='Emit machine-readable JSON to stdout.')] = False,
    ) -> None:
        """Show the result of one Jenkins build of this pipeline, the most recent by default.

        Exits 0 while the build runs or once it succeeded, and 3 when it finished any other way."""
        resp = jenkins_request(config, 'GET', f'{build_url_path(resolved(), build)}/api/json', 200)
        data = resp.json()
        building = data.get('building', False)
        result = data.get('result', 'IN PROGRESS' if building else 'UNKNOWN')
        number = data.get('number', '?')
        display = data.get('displayName', f'#{number}')
        exit_code = 0 if building or result == 'SUCCESS' else 3

        if as_json:
            emit_json({'build': display, 'number': number, 'building': building, 'result': result})
        elif building:
            info(f'{display} - [yellow]BUILDING[/yellow]')
        elif result == 'SUCCESS':
            success(f'{display} - {result}')
        else:
            error(f'{display} - {result}')

        if exit_code:
            raise typer.Exit(exit_code)

    @jenkins_app.command(epilog=f'Example:\n\ndectl {pipeline_name} jenkins logs 42 --follow')
    def logs(
        build: Annotated[int | None, build_argument] = None,
        follow: Annotated[bool, typer.Option('--follow', '-f', help='Keep tailing while the build is still running')] = False,
    ) -> None:
        """Show (and optionally tail) console output from one build, the most recent by default."""
        stream_console(resolved(), build, follow=follow)

    def strip_jenkins_noise(text: str) -> str:
        text = re.sub(r'\x1b\[8m.*?\x1b\[0m', '', text)
        lines = text.splitlines(keepends=True)
        return ''.join(line for line in lines if not line.strip().startswith('[Pipeline]'))

    def stream_console(job: JenkinsJobConfig, build: int | None, follow: bool = True) -> None:
        offset = 0

        while True:
            resp = jenkins_request(config, 'GET', f'{build_url_path(job, build)}/logText/progressiveText?start={offset}', 200)
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
