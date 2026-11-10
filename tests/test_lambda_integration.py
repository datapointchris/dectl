"""Live AWS test for the Lambda deploy path.

Opt-in: `uv run pytest --run-integration`. It creates a throwaway function with a `live` alias,
deploys to it through dectl, and deletes it. Nothing is invoked, so it costs nothing.

The credentials need the Lambda actions and `iam:PassRole` on the role `DECTL_IT_LAMBDA_ROLE_ARN`
names, or `iam:CreateRole` when it names none, because the fixture then makes its own.
"""

import contextlib
import io
import time
import uuid
import zipfile
from types import SimpleNamespace

import botocore.exceptions
import pytest

from dectl.commands.lambda_ import make_lambda_app
from dectl.config import DectlConfig
from tests.conftest import RefusalRunner

pytestmark = pytest.mark.integration

runner = RefusalRunner()


def archive(returns: str) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w') as zipped:
        zipped.writestr('handler.py', f'def handler(event, context):\n    return {returns!r}\n')
    return buffer.getvalue()


@contextlib.contextmanager
def live_function(session, role_arn: str):
    """A function with a published version 1 and a `live` alias on it, deleted whatever the test does.

    Lambda refuses a role it cannot assume yet with InvalidParameterValueException, and a new
    role takes seconds to propagate, so creation retries that case for up to 90 seconds."""
    client = session.client('lambda')
    name = f'dectl-it-{uuid.uuid4().hex[:8]}'
    deadline = time.time() + 90
    while True:
        try:
            client.create_function(
                FunctionName=name, Runtime='python3.12', Role=role_arn, Handler='handler.handler', Code={'ZipFile': archive('first')}
            )
            break
        except botocore.exceptions.ClientError as exc:
            if exc.response['Error']['Code'] == 'InvalidParameterValueException' and time.time() < deadline:
                time.sleep(5)
                continue
            raise
    try:
        client.get_waiter('function_active_v2').wait(FunctionName=name)
        client.create_alias(FunctionName=name, Name='live', FunctionVersion=client.publish_version(FunctionName=name)['Version'])
        yield name
    finally:
        client.delete_function(FunctionName=name)


class OtherDeployDuringTheWait:
    """The real Lambda client, with a second deploy uploading its code while dectl waits on its own."""

    def __init__(self, client, other_code: bytes) -> None:
        self.client = client
        self.other_code = other_code

    def __getattr__(self, name):
        return getattr(self.client, name)

    def get_waiter(self, name):
        waiter = self.client.get_waiter(name)

        def wait(**kwargs):
            # Lambda refuses a second update while the first is in progress, so the other
            # deploy lands after dectl's settles and before dectl publishes.
            waiter.wait(**kwargs)
            self.client.update_function_code(FunctionName=kwargs['FunctionName'], ZipFile=self.other_code)
            waiter.wait(**kwargs)

        return SimpleNamespace(wait=wait)


def test_publish_moves_the_alias_to_the_uploaded_code_and_refuses_code_another_deploy_replaced(
    session, lambda_role_arn, tmp_path, monkeypatch
):
    """Only Lambda says whether the hash `UpdateFunctionCode` returns still matches after the
    waiter, and which error a hash another deploy replaced is refused with."""
    (tmp_path / 'code').mkdir()
    (tmp_path / 'code' / 'handler.py').write_text('def handler(event, context):\n    return "deployed"\n')
    client = session.client('lambda')

    with live_function(session, lambda_role_arn) as name:
        config = DectlConfig.model_validate(
            {
                'defaults': {'account_id': '1'},
                'pipelines': {
                    'proj': {
                        'resolve_paths_from': str(tmp_path),
                        'lambdas': {'fn': {'name': name, 'source_dir': 'code', 'live_alias': 'live'}},
                    }
                },
            }
        )
        app = make_lambda_app('proj', config.pipelines['proj'], config)
        monkeypatch.setattr('dectl.commands.lambda_.make_session', lambda _config: session)

        published = runner.invoke(app, ['fn', 'deploy', '--publish'])

        assert published.exit_code == 0, published.stderr
        live_version = client.get_alias(FunctionName=name, Name='live')['FunctionVersion']
        assert live_version != '1'
        latest = client.get_function_configuration(FunctionName=name)['CodeSha256']
        assert client.get_function_configuration(FunctionName=name, Qualifier=live_version)['CodeSha256'] == latest

        (tmp_path / 'code' / 'handler.py').write_text('def handler(event, context):\n    return "mine"\n')
        racing = SimpleNamespace(client=lambda service, **_kwargs: OtherDeployDuringTheWait(client, archive('theirs')))
        monkeypatch.setattr('dectl.commands.lambda_.make_session', lambda _config: racing)

        refused = runner.invoke(app, ['fn', 'deploy', '--publish'])

        assert refused.exit_code == 1
        assert 'no version was published' in ' '.join(refused.stderr.split())
        assert client.get_alias(FunctionName=name, Name='live')['FunctionVersion'] == live_version
