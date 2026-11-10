from typing import NamedTuple

import httpx2
import pytest
import typer

from dectl.commands.jenkins import make_jenkins_app
from dectl.config import DectlConfig
from dectl.env import set_active_environment
from dectl.main import DectlGroup
from tests.conftest import RefusalRunner

runner = RefusalRunner()

JENKINS_URL = 'https://jenkins.example.com'
JOB_PATH = '/job/de/job/proj-deploy'


class FinishedBuild(NamedTuple):
    log: str
    result: str


class FakeJenkins:
    """One job on a Jenkins server, answering the REST calls dectl makes as Jenkins does.

    A POST to `buildWithParameters` answers 201 with the queue item in `Location`, and the
    build gets its number only when the item leaves the queue. Until then `lastBuild` is the
    previous build, finished, which is what a reader keyed on `lastBuild` reports instead.
    The item leaves the queue only after it has been polled, so a caller that never reads it
    never sees the build start."""

    QUEUE_ITEM = 42

    def __init__(self, polls_before_start: int = 1) -> None:
        self.builds = {7: FinishedBuild('Finished: SUCCESS (build 7)\n', 'SUCCESS')}
        self.queue_polls = 0
        self.queued_build: int | None = None
        self.polls_before_start = polls_before_start
        self.requests: list[tuple[str, str, dict]] = []

    def handle(self, request: httpx2.Request) -> httpx2.Response:
        path = request.url.path
        if request.method == 'POST':
            self.requests.append(('POST', path, dict(httpx2.QueryParams(request.content.decode()))))
            assert path == f'{JOB_PATH}/buildWithParameters'
            return httpx2.Response(201, headers={'Location': f'{JENKINS_URL}/queue/item/{self.QUEUE_ITEM}/'})
        if path == f'/queue/item/{self.QUEUE_ITEM}/api/json':
            return httpx2.Response(200, json=self.queue_item())
        build, _, rest = path.removeprefix(f'{JOB_PATH}/').partition('/')
        number = max(self.builds) if build == 'lastBuild' else int(build)
        if number not in self.builds:
            return httpx2.Response(404, text='Not Found')
        record = self.builds[number]
        if rest == 'api/json':
            return httpx2.Response(200, json={'number': number, 'building': False, 'result': record.result})
        assert rest == 'logText/progressiveText'
        start = int(request.url.params.get('start', 0))
        headers = {'X-Text-Size': str(len(record.log)), 'X-More-Data': 'false'}
        return httpx2.Response(200, text=record.log[start:], headers=headers)

    def queue_item(self) -> dict:
        self.queue_polls += 1
        if self.queue_polls <= self.polls_before_start:
            return {'_class': 'hudson.model.Queue$WaitingItem', 'id': self.QUEUE_ITEM, 'why': 'In the quiet period. Expires in 4.9 sec'}
        if self.queued_build is None:
            self.queued_build = max(self.builds) + 1
            self.builds[self.queued_build] = FinishedBuild('Finished: FAILURE (the queued build)\n', 'FAILURE')
        executable = {'number': self.queued_build, 'url': f'{JENKINS_URL}{JOB_PATH}/{self.queued_build}/'}
        return {'_class': 'hudson.model.Queue$LeftItem', 'id': self.QUEUE_ITEM, 'cancelled': False, 'executable': executable}


@pytest.fixture
def jenkins(monkeypatch) -> FakeJenkins:
    fake = FakeJenkins()
    client = httpx2.Client
    monkeypatch.setattr(httpx2, 'Client', lambda **kwargs: client(transport=httpx2.MockTransport(fake.handle), **kwargs))
    monkeypatch.setattr('dectl.commands.jenkins.time.sleep', lambda _seconds: None)
    return fake


def jenkins_config(**job) -> DectlConfig:
    return DectlConfig.model_validate(
        {
            'defaults': {'account_id': '123456789012'},
            'jenkins': {'url': JENKINS_URL, 'user': 'user', 'token': 'token'},
            'pipelines': {'proj': {'jenkins': {'job_path': 'de/proj-deploy', **job}}},
        }
    )


@pytest.mark.parametrize(
    ('argv', 'actions'),
    [([], []), (['apply'], ['deploy']), (['plan'], ['dry_run'])],
    ids=['bare-jenkins', 'jenkins-apply', 'jenkins-plan'],
)
def test_each_configured_action_queues_its_own_parameters_for_the_requested_environment(jenkins, argv, actions):
    """A bare `jenkins`, entered before the action is typed, must queue no build.

    `plan` posting the apply's parameters applies Terraform, and an unsubstituted `{env}` names
    no environment. Jenkins is the only thing between this command and production, so the
    parameters it is handed are the whole of what the command decides."""
    config = jenkins_config(
        parameters={'ENVIRONMENT': '{env}'},
        actions={'apply': {'ACTION': 'deploy'}, 'plan': {'ACTION': 'dry_run'}},
    )
    set_active_environment('prod', '--env')

    result = runner.invoke(make_jenkins_app('proj', config.pipelines['proj'], config), argv)

    assert result.exit_code == (0 if actions else 2)
    path = f'{JOB_PATH}/buildWithParameters'
    assert jenkins.requests == [('POST', path, {'ENVIRONMENT': 'prod', 'ACTION': action}) for action in actions]
    if actions:
        assert 'build #8' in result.output


def test_follow_streams_the_build_its_post_queued_rather_than_the_last_one(jenkins):
    """While the queued build waits, `lastBuild` is the previous one, finished.

    Streaming that reports another build's result as this deploy's, which is read once and
    believed. The build's number is the handle `status` and `logs` take afterwards."""
    config = jenkins_config(actions={'apply': {'ACTION': 'deploy'}})
    app = make_jenkins_app('proj', config.pipelines['proj'], config)

    followed = runner.invoke(app, ['apply', '--follow'])
    status = runner.invoke(app, ['status', '8', '--json'])

    assert 'the queued build' in followed.output
    assert 'build 7' not in followed.output
    assert status.exit_code == 3
    assert '"number": 8' in status.output


class LastBuild:
    status_code = 200

    def __init__(self, building, result):
        self.body = {'number': 7, 'building': building, 'result': result}

    def json(self):
        return self.body


@pytest.mark.parametrize('as_json', [[], ['--json']], ids=['human', 'json'])
@pytest.mark.parametrize(
    ('building', 'build_result', 'exit_code'),
    [(False, 'SUCCESS', 0), (True, None, 0), (False, 'FAILURE', 3), (False, 'ABORTED', 3)],
    ids=['succeeded', 'building', 'failed', 'aborted'],
)
def test_status_exits_3_when_the_last_build_finished_without_succeeding(monkeypatch, as_json, building, build_result, exit_code):
    """`jenkins status && next-step` must stop on a failed build without reading its output."""
    config = DectlConfig.model_validate(
        {
            'defaults': {'account_id': '123456789012'},
            'jenkins': {'url': 'https://jenkins.example.com', 'user': 'user', 'token': 'token'},
            'pipelines': {'proj': {'jenkins': {'job_path': 'de/proj-deploy'}}},
        }
    )
    monkeypatch.setattr('dectl.commands.jenkins.jenkins_request', lambda *_a, **_k: LastBuild(building, build_result))

    result = runner.invoke(make_jenkins_app('proj', config.pipelines['proj'], config), ['status', *as_json])

    assert result.exit_code == exit_code


def refuse_connection(request):
    raise httpx2.ConnectError('connection refused', request=request)


def answer_not_found(_request):
    return httpx2.Response(404, text='Not Found')


@pytest.mark.parametrize('handler', [refuse_connection, answer_not_found], ids=['transport-failure', 'unexpected-status'])
def test_a_failed_jenkins_request_ends_at_the_root_handler_rather_than_a_traceback(monkeypatch, handler):
    """Every Jenkins verb, and the next one added, refuses through `jenkins_request` alone.

    `RefusalRunner` re-raises anything but an exit, so an escaped transport error, or a
    `JenkinsError` the root group does not catch, fails this test by name."""
    config = DectlConfig.model_validate(
        {
            'defaults': {'account_id': '123456789012'},
            'jenkins': {'url': 'https://jenkins.example.com', 'user': 'user', 'token': 'token'},
            'pipelines': {'proj': {'jenkins': {'job_path': 'de/proj-deploy'}}},
        }
    )
    client = httpx2.Client
    monkeypatch.setattr(httpx2, 'Client', lambda **kwargs: client(transport=httpx2.MockTransport(handler), **kwargs))
    root = typer.Typer(cls=DectlGroup)
    root.callback()(lambda: None)
    root.add_typer(make_jenkins_app('proj', config.pipelines['proj'], config), name='jenkins')

    result = runner.invoke(root, ['jenkins', 'status'])

    assert result.exit_code == 1
