import httpx2
import pytest
import typer

from dectl.commands.jenkins import make_jenkins_app
from dectl.config import DectlConfig
from dectl.env import set_active_environment
from dectl.main import DectlGroup
from tests.conftest import RefusalRunner

runner = RefusalRunner()


class QueuedBuild:
    status_code = 201
    text = ''


@pytest.mark.parametrize(
    ('argv', 'actions'),
    [([], []), (['apply'], ['deploy']), (['plan'], ['dry_run'])],
    ids=['bare-jenkins', 'jenkins-apply', 'jenkins-plan'],
)
def test_each_configured_action_queues_its_own_parameters_for_the_requested_environment(monkeypatch, argv, actions):
    """A bare `jenkins`, entered before the action is typed, must queue no build.

    `plan` posting the apply's parameters applies Terraform, and an unsubstituted `{env}` names
    no environment. Jenkins is the only thing between this command and production, so the
    parameters it is handed are the whole of what the command decides."""
    config = DectlConfig.model_validate(
        {
            'defaults': {'account_id': '123456789012'},
            'jenkins': {'url': 'https://jenkins.example.com', 'user': 'user', 'token': 'token'},
            'pipelines': {
                'proj': {
                    'jenkins': {
                        'job_path': 'de/proj-deploy',
                        'parameters': {'ENVIRONMENT': '{env}'},
                        'actions': {'apply': {'ACTION': 'deploy'}, 'plan': {'ACTION': 'dry_run'}},
                    }
                }
            },
        }
    )
    requests = []

    def jenkins_request(_config, method, path, _expected_status, **kwargs):
        requests.append((method, path, kwargs))
        return QueuedBuild()

    monkeypatch.setattr('dectl.commands.jenkins.jenkins_request', jenkins_request)
    set_active_environment('prod', '--env')

    result = runner.invoke(make_jenkins_app('proj', config.pipelines['proj'], config), argv)

    assert result.exit_code == (0 if actions else 2)
    path = '/job/de/job/proj-deploy/buildWithParameters'
    assert requests == [('POST', path, {'data': {'ENVIRONMENT': 'prod', 'ACTION': action}}) for action in actions]


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
