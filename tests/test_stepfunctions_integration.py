"""Live AWS round trip for the state machine deploy.

Opt-in: `uv run pytest --run-integration`. It creates a Standard state machine of Pass states with
a published version and a `live` alias, deploys the committed layered config's JSONata flow over
it with dectl, and deletes the machine, which takes its versions and aliases with it. The deploy
has the service validate the definition before writing it, which parses every JSONata expression,
so a substitution rendering one the service refuses fails here. Creating, updating and publishing
a state machine cost nothing, and no execution is started.

A state machine needs a role Step Functions can assume. Name one in `DECTL_IT_STATES_ROLE_ARN` and
the credentials need `iam:PassRole` on it; name none and they need `iam:CreateRole`.
"""

import json
import time
import uuid

import pytest
from botocore.exceptions import ClientError

from dectl.asl import render_definition
from dectl.commands.stepfunctions import make_sfn_app
from dectl.config import DectlConfig
from dectl.config import load_config
from dectl.env import render_env_model
from tests.conftest import LAYERED_CONFIG
from tests.conftest import RefusalRunner

pytestmark = pytest.mark.integration

runner = RefusalRunner()

PASS_DEFINITION = json.dumps({'StartAt': 'Done', 'States': {'Done': {'Type': 'Pass', 'End': True}}})
# Formatted as a hand-edited file is, so a definition Step Functions normalized would differ.
FLOW_TEMPLATE = (LAYERED_CONFIG.parent / 'flow.asl.json.tftpl').read_text()


def layered_substitutions(monkeypatch) -> dict[str, str]:
    """finance's substitutions as the committed config resolves them: variables, a YAML merge,
    a number, a boolean and two whole JSONata expressions."""
    monkeypatch.setattr('dectl.config.CONFIG_PATH', LAYERED_CONFIG)
    config = load_config()
    assert config is not None
    return render_env_model(config.pipelines['finance'].step_functions['flow']).definition_substitutions


def create_machine_with_role_retry(sfn, **create_kwargs) -> dict:
    # A new role is not assumable by Step Functions until IAM has propagated it, and the refusal
    # names the assume rather than carrying a code of its own.
    deadline = time.time() + 90
    while True:
        try:
            return sfn.create_state_machine(**create_kwargs)
        except ClientError as exc:
            if 'assume' in exc.response['Error']['Message'].lower() and time.time() < deadline:
                time.sleep(5)
                continue
            raise


@pytest.fixture
def live_machine(session, states_role_arn):
    sfn = session.client('stepfunctions')
    name = f'dectl-it-{uuid.uuid4().hex[:8]}'
    created = create_machine_with_role_retry(
        sfn, name=name, definition=PASS_DEFINITION, roleArn=states_role_arn, type='STANDARD', publish=True
    )
    sfn.create_state_machine_alias(
        name='live', routingConfiguration=[{'stateMachineVersionArn': created['stateMachineVersionArn'], 'weight': 100}]
    )
    try:
        yield created['stateMachineArn']
    finally:
        sfn.delete_state_machine(stateMachineArn=created['stateMachineArn'])


def sfn_app(session, tmp_path, monkeypatch, machine_name: str, template: str, substitutions: dict[str, str]):
    (tmp_path / 'flow.asl.json').write_text(template)
    account = session.client('sts').get_caller_identity()['Account']
    config = DectlConfig.model_validate(
        {
            'defaults': {'account_id': account, 'region': session.region_name},
            'pipelines': {
                'it': {
                    'resolve_paths_from': str(tmp_path),
                    'step_functions': {
                        'flow': {
                            'name': machine_name,
                            'definition_file': 'flow.asl.json',
                            'definition_substitutions': substitutions,
                            'live_alias': 'live',
                        }
                    },
                }
            },
        }
    )
    monkeypatch.setattr('dectl.commands.stepfunctions.make_session', lambda _config: session)
    return make_sfn_app('it', config.pipelines['it'], config)


def test_a_deploy_refuses_writes_converges_and_publishes(session, live_machine, tmp_path, monkeypatch):
    sfn = session.client('stepfunctions')
    machine_name = live_machine.rsplit(':', 1)[-1]
    substitutions = layered_substitutions(monkeypatch)
    rendered = render_definition(FLOW_TEMPLATE, substitutions).text
    # A machine never updated carries no revisionId at all, so its absence is the before-image.
    assert 'revisionId' not in sfn.describe_state_machine(stateMachineArn=live_machine)

    # The fake's validation rule, held to the real service: no StartAt is refused, and nothing is written.
    broken = sfn_app(session, tmp_path, monkeypatch, machine_name, '{"States": {}}', substitutions)
    assert runner.invoke(broken, ['flow', 'deploy']).exit_code == 1
    assert 'revisionId' not in sfn.describe_state_machine(stateMachineArn=live_machine)

    app = sfn_app(session, tmp_path, monkeypatch, machine_name, FLOW_TEMPLATE, substitutions)
    result = runner.invoke(app, ['flow', 'deploy'])
    assert result.exit_code == 0, result.output
    deployed = sfn.describe_state_machine(stateMachineArn=live_machine)
    # Byte for byte, which is what lets Terraform's next plan read the deploy as no change.
    assert deployed['definition'] == rendered

    assert runner.invoke(app, ['flow', 'deploy']).exit_code == 0
    assert sfn.describe_state_machine(stateMachineArn=live_machine)['revisionId'] == deployed['revisionId']

    assert runner.invoke(app, ['flow', 'deploy', '--publish']).exit_code == 0
    [route] = settled_routing(sfn, f'{live_machine}:live', away_from=f'{live_machine}:1')
    assert route['weight'] == 100
    assert sfn.describe_state_machine(stateMachineArn=route['stateMachineVersionArn'])['definition'] == rendered


def settled_routing(sfn, alias_arn: str, away_from: str) -> list[dict]:
    """The alias's routing once no route names `away_from`, or as it stands after 30 seconds.

    A read straight after `UpdateStateMachineAlias` returns the previous routing: measured, the
    old version at once and the new one five seconds later."""
    deadline = time.time() + 30
    while True:
        routing = sfn.describe_state_machine_alias(stateMachineAliasArn=alias_arn)['routingConfiguration']
        if all(route['stateMachineVersionArn'] != away_from for route in routing) or time.time() > deadline:
            return routing
        time.sleep(2)
