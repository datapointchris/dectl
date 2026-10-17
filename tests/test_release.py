import pytest

from dectl.commands.release import make_release_app
from dectl.config import DectlConfig
from dectl.env import set_active_environment
from tests.conftest import RefusalRunner

runner = RefusalRunner()


class QueuedBuild:
    status_code = 201
    text = ''


@pytest.mark.parametrize(
    ('argv', 'actions'),
    [([], []), (['deploy'], ['deploy']), (['plan'], ['dry_run'])],
    ids=['bare-release', 'release-deploy', 'release-plan'],
)
def test_only_a_named_release_verb_queues_a_build_for_the_requested_environment(monkeypatch, argv, actions):
    """A bare `release`, entered before `plan` is typed, must queue no terraform apply.

    `release plan` queueing a deploy applies Terraform, and an unsubstituted `{env}` names no
    environment. Jenkins is the only thing between this command and production, so the
    parameters it is handed are the whole of what the command decides."""
    config = DectlConfig.model_validate(
        {
            'defaults': {'account_id': '123456789012'},
            'jenkins': {'url': 'https://jenkins.example.com', 'user': 'user', 'token': 'token'},
            'pipelines': {'proj': {'jenkins': {'job_path': 'de/proj-deploy', 'parameters': {'ENVIRONMENT': '{env}'}}}},
        }
    )
    requests = []

    def jenkins_request(_config, method, path, **kwargs):
        requests.append((method, path, kwargs))
        return QueuedBuild()

    monkeypatch.setattr('dectl.commands.release.jenkins_request', jenkins_request)
    set_active_environment('prod', '--env')

    result = runner.invoke(make_release_app('proj', config.pipelines['proj'].jenkins, config), argv)

    assert result.exit_code == (0 if actions else 2)
    path = '/job/de/job/proj-deploy/buildWithParameters'
    assert requests == [('POST', path, {'data': {'ENVIRONMENT': 'prod', 'ACTION': action}}) for action in actions]
