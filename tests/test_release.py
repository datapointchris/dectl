import pytest

from dectl.commands.release import make_release_app
from dectl.config import DectlConfig
from tests.conftest import RefusalRunner

runner = RefusalRunner()


class QueuedBuild:
    status_code = 201
    text = ''


@pytest.mark.parametrize(('argv', 'action'), [([], 'deploy'), (['plan'], 'dry_run')], ids=['release', 'release-plan'])
def test_each_release_verb_queues_its_own_action(monkeypatch, argv, action):
    """`release plan` queueing a deploy applies Terraform rather than planning it.

    Jenkins is the only thing between this command and production, so the parameters it is
    handed are the whole of what the command decides."""
    config = DectlConfig.model_validate(
        {
            'defaults': {'account_id': '123456789012'},
            'jenkins': {'url': 'https://jenkins.example.com', 'user': 'user', 'token': 'token'},
            'pipelines': {'proj': {'jenkins': {'job_path': 'de/proj-deploy', 'parameters': {'ENVIRONMENT': 'prod'}}}},
        }
    )
    requests = []

    def jenkins_request(_config, method, path, **kwargs):
        requests.append((method, path, kwargs))
        return QueuedBuild()

    monkeypatch.setattr('dectl.commands.release.jenkins_request', jenkins_request)

    result = runner.invoke(make_release_app('proj', config.pipelines['proj'].jenkins, config), argv)

    assert result.exit_code == 0
    assert requests == [('POST', '/job/de/job/proj-deploy/buildWithParameters', {'data': {'ENVIRONMENT': 'prod', 'ACTION': action}})]
