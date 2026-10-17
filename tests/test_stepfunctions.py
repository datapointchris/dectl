import json

import pytest
import typer
from botocore.exceptions import ClientError

from dectl.asl import Suggestion
from dectl.asl import TemplateFault
from dectl.asl import render_definition
from dectl.asl import suggest_substitutions
from dectl.commands.stepfunctions import make_sfn_app
from dectl.commands.stepfunctions import suggested_entry
from dectl.config import DectlConfig
from dectl.config import StepFunctionConfig
from tests.conftest import RefusalRunner

runner = RefusalRunner()


def make_config(step_functions: dict) -> DectlConfig:
    return DectlConfig.model_validate(
        {
            'defaults': {'account_id': '123456789012', 'region': 'us-east-2'},
            'pipelines': {'proj': {'step_functions': step_functions}},
        }
    )


@pytest.mark.parametrize(
    ('machine', 'verbs'),
    [
        ({'name': 'my-flow'}, {'run', 'logs', 'runs'}),
        ({'name': 'my-flow', 'definition_file': 'flow.asl.json'}, {'deploy', 'run', 'logs', 'runs'}),
    ],
    ids=['no-definition-file', 'definition-file'],
)
def test_deploy_exists_only_for_a_machine_naming_its_definition_file(machine, verbs):
    config = make_config({'flow': machine})
    tree = typer.main.get_command(make_sfn_app('proj', config.pipelines['proj'], config))

    assert set(tree.commands['flow'].commands) == verbs


# Each template beside what Terraform 1.10.5's templatefile rendered from it, with these vars.
TERRAFORM_RENDERINGS = [
    pytest.param(
        """{"A": "${fn_arn}", "B": "${ fn_arn }", "Ctx.$": "$$.Execution.Id", "P.$": "$.input", """
        """"Lit": "$${not_a_var}", "Pct": "%%{x}", "Fmt.$": "States.Format('{}', $.x)", "N": ${timeout}}""",
        {'fn_arn': 'arn:aws:lambda:x', 'timeout': 30},
        """{"A": "arn:aws:lambda:x", "B": "arn:aws:lambda:x", "Ctx.$": "$$.Execution.Id", "P.$": "$.input", """
        """"Lit": "${not_a_var}", "Pct": "%{x}", "Fmt.$": "States.Format('{}', $.x)", "N": 30}""",
        id='asl-syntax-and-escapes',
    ),
    pytest.param(
        '{"a": "$$${fn}", "b": "$$$${fn}", "c": "%%%{x}", "d": "${fn}${fn}", "e": "x$${fn}y"}',
        {'fn': 'F'},
        '{"a": "$${fn}", "b": "$$${fn}", "c": "%%{x}", "d": "FF", "e": "x${fn}y"}',
        id='runs-of-dollars',
    ),
    pytest.param(
        '{"a": ${a}, "b": ${b}, "c": ${c}, "d": ${d}, "e": ${e}, "f": ${f}, "g": ${g}, "h": "${h}", "i": ${i}}',
        {'a': 300, 'b': 2.0, 'c': 1.5, 'd': 0.0000001, 'e': True, 'f': False, 'g': 1e20, 'h': 2.50, 'i': -0.25},
        '{"a": 300, "b": 2, "c": 1.5, "d": 0.0000001, "e": true, "f": false, "g": 100000000000000000000, "h": "2.5", "i": -0.25}',
        id='numeric-and-boolean-placeholders-from-yaml',
    ),
]


@pytest.mark.parametrize(('template', 'substitutions', 'terraform_rendered'), TERRAFORM_RENDERINGS)
def test_a_definition_renders_to_the_bytes_terraform_renders(template, substitutions, terraform_rendered):
    # Through the model, which is where a YAML number or boolean becomes Terraform's text for it.
    loaded = StepFunctionConfig(name='flow', definition_substitutions=substitutions).definition_substitutions

    rendering = render_definition(template, loaded)

    assert rendering.problems == []
    assert rendering.text == terraform_rendered


def test_every_placeholder_dectl_cannot_render_is_named_with_its_line():
    template = '{\n  "A": "${missing}",\n  "B": "${upper(x)}",\n  "C": "%{ if x }y%{ endif }",\n  "D": "${never'

    problems = render_definition(template, {}).problems

    assert [(p.fault, p.line, p.name) for p in problems] == [
        (TemplateFault.MISSING_SUBSTITUTION, 2, 'missing'),
        (TemplateFault.NOT_A_BARE_NAME, 3, ''),
        (TemplateFault.DIRECTIVE, 4, ''),
        (TemplateFault.DIRECTIVE, 4, ''),
        (TemplateFault.UNCLOSED, 5, ''),
    ]


def test_a_missing_substitution_is_suggested_from_where_it_sits_in_the_live_definition():
    """A first deploy against a machine Terraform built, with no definition_substitutions yet.

    Every placeholder is missing, `${timeout}` sits outside a string, and each suggestion has to
    paste as an entry that still names the right resource under another `--env`."""
    template = (
        '{"StartAt": "Copy", "States": {\n'
        '  "Copy": {"Type": "Task", "Parameters": {"JobName": "${copy_job}"}, "TimeoutSeconds": ${timeout}, "Next": "Notify"},\n'
        '  "Notify": {"Type": "Task", "Parameters": {"FunctionName": "arn:aws:lambda:us-east-2:1:function:${notify_name}"},'
        ' "Next": "Audit"},\n'
        # A state the edit added, so the live definition has nothing where its placeholder sits.
        '  "Audit": {"Type": "Task", "Parameters": {"FunctionName": "${audit_fn}"}, "End": true}}}'
    )
    live = {
        'StartAt': 'Copy',
        'States': {
            'Copy': {'Type': 'Task', 'Parameters': {'JobName': 'sales-dev-copy'}, 'TimeoutSeconds': 3600, 'Next': 'Notify'},
            'Notify': {'Type': 'Task', 'Parameters': {'FunctionName': 'arn:aws:lambda:us-east-2:1:function:sales-dev-notify'}, 'End': True},
        },
    }

    suggestions = suggest_substitutions(template, {}, json.dumps(live))

    assert suggestions == {
        'copy_job': Suggestion('sales-dev-copy', '/States/Copy/Parameters/JobName'),
        'timeout': Suggestion(3600, '/States/Copy/TimeoutSeconds'),
        'notify_name': Suggestion('sales-dev-notify', '/States/Notify/Parameters/FunctionName'),
    }
    assert [suggested_entry(name, suggestion.value) for name, suggestion in suggestions.items()] == [
        'copy_job: sales-{env}-copy',
        'timeout: 3600',
        'notify_name: sales-{env}-notify',
    ]


class FakeStepFunctions:
    """The Step Functions calls a deploy makes, enforcing the rules the deploy relies on.

    A publish carrying a revision other than the current one is refused, as is an alias that does
    not exist or a routing whose weights do not sum to 100. An update needs a definition or a role.
    A machine never updated has no `revisionId` at all, which is what the live service returns.
    Every write is recorded in `writes`, so a refusal is checked by asserting it is empty."""

    def __init__(self, arn: str, definition: str, aliases: tuple[str, ...] = ()) -> None:
        self.arn = arn
        self.definition = definition
        self.revision = 0
        self.versions: dict[str, str] = {}
        self.aliases: dict[str, list[dict]] = {f'{arn}:{name}': [] for name in aliases}
        self.writes: list[tuple[str, dict]] = []

    @staticmethod
    def client_error(code: str, operation: str) -> ClientError:
        return ClientError({'Error': {'Code': code, 'Message': code}}, operation)

    def describe_state_machine(self, stateMachineArn):
        if stateMachineArn != self.arn:
            raise self.client_error('StateMachineDoesNotExist', 'DescribeStateMachine')
        described = {'definition': self.definition, 'type': 'STANDARD'}
        if self.revision:
            described['revisionId'] = f'rev-{self.revision}'
        return described

    def validate_state_machine_definition(self, definition, type, severity):
        assert type in ('STANDARD', 'EXPRESS')
        parsed = json.loads(definition)
        if 'StartAt' not in parsed:
            diagnostic = {'severity': 'ERROR', 'code': 'SCHEMA_VALIDATION_FAILED', 'message': 'missing StartAt', 'location': '/'}
            return {'result': 'FAIL', 'diagnostics': [diagnostic]}
        return {'result': 'OK', 'diagnostics': []}

    def update_state_machine(self, **kwargs):
        if 'definition' not in kwargs and 'roleArn' not in kwargs:
            raise self.client_error('MissingRequiredParameter', 'UpdateStateMachine')
        self.writes.append(('update_state_machine', kwargs))
        self.definition = kwargs['definition']
        self.revision += 1
        return {'revisionId': f'rev-{self.revision}'}

    def publish_state_machine_version(self, stateMachineArn, revisionId=None):
        if revisionId is not None and (not self.revision or revisionId != f'rev-{self.revision}'):
            raise self.client_error('ConflictException', 'PublishStateMachineVersion')
        self.writes.append(('publish_state_machine_version', {'revisionId': revisionId}))
        published = next((arn for arn, text in self.versions.items() if text == self.definition), None)
        if published is None:
            published = f'{self.arn}:{len(self.versions) + 1}'
            self.versions[published] = self.definition
        return {'stateMachineVersionArn': published}

    def describe_state_machine_alias(self, stateMachineAliasArn):
        if stateMachineAliasArn not in self.aliases:
            raise self.client_error('ResourceNotFound', 'DescribeStateMachineAlias')
        return {'routingConfiguration': self.aliases[stateMachineAliasArn]}

    def update_state_machine_alias(self, stateMachineAliasArn, routingConfiguration):
        if stateMachineAliasArn not in self.aliases:
            raise self.client_error('ResourceNotFound', 'UpdateStateMachineAlias')
        assert sum(route['weight'] for route in routingConfiguration) == 100
        assert all(route['stateMachineVersionArn'] in self.versions for route in routingConfiguration)
        self.writes.append(('update_state_machine_alias', {'routingConfiguration': routingConfiguration}))
        self.aliases[stateMachineAliasArn] = routingConfiguration


class FakeSession:
    def __init__(self, client: FakeStepFunctions) -> None:
        self.stepfunctions = client

    def client(self, name: str) -> FakeStepFunctions:
        assert name == 'stepfunctions'
        return self.stepfunctions


MACHINE_ARN = 'arn:aws:states:us-east-2:123456789012:stateMachine:sales-dev-flow'
# Formatted the way a hand-edited file is, so a deploy that re-serialized it would differ.
TEMPLATE = '{\n  "StartAt": "Notify",\n  "States": {\n    "Notify": {"Type": "Task", "Resource": "${notify_arn}", "End": true}\n  }\n}\n'
NOTIFY_ARN = 'arn:aws:lambda:us-east-2:123456789012:function:sales-dev-notify'
RENDERED = TEMPLATE.replace('${notify_arn}', NOTIFY_ARN)
OLD_DEFINITION = json.dumps({'StartAt': 'Notify', 'States': {'Notify': {'Type': 'Pass', 'End': True}}})


def deploy_app(monkeypatch, tmp_path, client, *, template=TEMPLATE, substitutions=None, live_alias='live'):
    (tmp_path / 'flow.asl.json').write_text(template)
    machine = {
        'name': 'sales-{env}-flow',
        'definition_file': 'flow.asl.json',
        'definition_substitutions': substitutions if substitutions is not None else {'notify_arn': NOTIFY_ARN.replace('dev', '{env}')},
        'live_alias': live_alias,
    }
    config = DectlConfig.model_validate(
        {
            'defaults': {'account_id': '123456789012', 'region': 'us-east-2'},
            'pipelines': {'proj': {'resolve_paths_from': str(tmp_path), 'step_functions': {'flow': machine}}},
        }
    )
    monkeypatch.setattr('dectl.commands.stepfunctions.make_session', lambda _config: FakeSession(client))
    return make_sfn_app('proj', config.pipelines['proj'], config)


def test_deploy_writes_the_rendered_bytes_and_only_the_definition(monkeypatch, tmp_path):
    client = FakeStepFunctions(MACHINE_ARN, OLD_DEFINITION)
    app = deploy_app(monkeypatch, tmp_path, client)

    result = runner.invoke(app, ['flow', 'deploy'])

    assert result.exit_code == 0
    assert client.writes == [('update_state_machine', {'stateMachineArn': MACHINE_ARN, 'definition': RENDERED})]


@pytest.mark.parametrize(
    ('live_definition', 'writes'),
    [
        (RENDERED, []),
        # A file re-indented, or rewritten by a JSON formatter: equal as JSON, unequal as text,
        # and the text is what Terraform's next plan compares.
        (
            json.dumps(json.loads(RENDERED), separators=(',', ':')),
            [('update_state_machine', {'stateMachineArn': MACHINE_ARN, 'definition': RENDERED})],
        ),
    ],
    ids=['identical-bytes', 'formatting-only-edit'],
)
def test_a_formatting_only_edit_is_written_and_identical_bytes_are_not(monkeypatch, tmp_path, live_definition, writes):
    client = FakeStepFunctions(MACHINE_ARN, live_definition)
    app = deploy_app(monkeypatch, tmp_path, client)

    result = runner.invoke(app, ['flow', 'deploy'])

    assert result.exit_code == 0
    assert client.writes == writes


@pytest.mark.parametrize(
    ('argv', 'deploy_kwargs', 'aliases', 'exit_code'),
    [
        (['--plan', '--publish'], {}, ('live',), 0),
        ([], {'template': '{"States": {}}'}, (), 1),
        ([], {'substitutions': {}}, (), 1),
        (['--publish'], {}, (), 1),
    ],
    ids=['plan', 'invalid-definition', 'missing-substitution', 'missing-alias'],
)
def test_a_rehearsed_or_refused_deploy_writes_nothing(monkeypatch, tmp_path, argv, deploy_kwargs, aliases, exit_code):
    client = FakeStepFunctions(MACHINE_ARN, OLD_DEFINITION, aliases=aliases)
    app = deploy_app(monkeypatch, tmp_path, client, **deploy_kwargs)

    result = runner.invoke(app, ['flow', 'deploy', *argv])

    assert result.exit_code == exit_code
    assert client.writes == []


@pytest.mark.parametrize('live_definition', [OLD_DEFINITION, RENDERED], ids=['changed', 'already-deployed'])
def test_publish_moves_the_alias_to_a_version_holding_the_rendered_definition(monkeypatch, tmp_path, live_definition):
    client = FakeStepFunctions(MACHINE_ARN, live_definition, aliases=('live',))
    app = deploy_app(monkeypatch, tmp_path, client)

    result = runner.invoke(app, ['flow', 'deploy', '--publish'])

    assert result.exit_code == 0
    [route] = client.aliases[f'{MACHINE_ARN}:live']
    assert route['weight'] == 100
    assert client.versions[route['stateMachineVersionArn']] == RENDERED
