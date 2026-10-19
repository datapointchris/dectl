"""The config's `variables` blocks, substituted into every string value before the models validate.

A variable is static, so it is replaced once, at load, in the raw mapping YAML parsed. `{env}` is
not: `--env` is parsed after the command tree is built, so it passes through untouched for
`env.substitute_env` to replace at each command. The top-level block reaches the whole file, and
a pipeline's block reaches that pipeline alone.

Nothing a variable could replace is overwritten. `env` cannot be a variable's name, a pipeline
cannot redefine a top-level variable, and a `{name}` with no variable defined for it is refused
rather than left in place to reach AWS as a literal name. `{{name}}` is the escape, and renders
as `{name}`. A YAML anchor is not a variable: `&raw` is repeated as `*raw`, never as `{raw}`.
Mapping keys are aliases and are never substituted.

Every string a substitution changed is recorded with what the file wrote, so a fault found later
in the substituted value can quote the line the reader has to edit.
"""

import difflib
import re
from typing import Any
from typing import NamedTuple

from pydantic import ValidationError
from pydantic_core import InitErrorDetails
from pydantic_core import PydanticCustomError

from dectl.values import ENV_PLACEHOLDER
from dectl.values import Written

NAME = re.compile(r'[A-Za-z_][A-Za-z0-9_-]*')
# A `$` before the brace makes it a Terraform or environment-variable placeholder, never a variable.
VARIABLE_TOKEN = re.compile(rf'(?<!\$)\{{\{{(?P<escaped>{NAME.pattern})\}}\}}|(?<!\$)\{{(?P<name>{NAME.pattern})\}}')
RESERVED_NAME = ENV_PLACEHOLDER.strip('{}')

type Location = tuple[str | int, ...]


class Variable(NamedTuple):
    value: str
    # Where it is defined, as a dotted key the reader can search the file for.
    origin: str
    # The variables its own value was built from, so a fault traces to the one holding the character.
    used: tuple[str, ...] = ()


class Definition(NamedTuple):
    origin: str
    # The value as the file wrote it, before any substitution.
    written: Any


class Expansion(NamedTuple):
    config: Any
    # Pipeline name -> location within that pipeline -> (substituted value, what the file wrote).
    writings: dict[str, dict[Location, tuple[str, Written]]]


def dotted(location: Location) -> str:
    return '.'.join(str(part) for part in location)


class Pass:
    """One expansion of one file: the faults it found and the strings it changed."""

    def __init__(self, defined: dict[str, list[Definition]], anchor_lines: dict[str, int]) -> None:
        self.faults: list[InitErrorDetails] = []
        self.changed: dict[Location, tuple[str, Written]] = {}
        # Every variable name in the file -> where each is defined, so a reference out of a
        # variable's reach is told where it is rather than that it does not exist.
        self.defined = defined
        # Every YAML anchor in the file -> its line, so `{raw}` written for `&raw` is told `*raw`.
        self.anchor_lines = anchor_lines

    def fault(self, kind: str, message: str, location: Location, value: Any) -> None:
        self.faults.append(InitErrorDetails(type=PydanticCustomError(kind, message), loc=location, input=value))

    def substitute(self, value: Any, scope: dict[str, Variable], location: Location, below: frozenset[str] = frozenset()) -> Any:
        """`value` with every `{name}` in its strings replaced from `scope`, at any depth.

        `below` holds the names defined later in the same `variables` block, which are the ones a
        reference can fail to see by being written too early."""
        if isinstance(value, dict):
            if unquoted_token(value):
                self.unquoted(value, scope, location)
                return value
            # Built new rather than edited: YAML hands an aliased block to every pipeline as one object.
            return {key: self.substitute(item, scope, (*location, key)) for key, item in value.items()}
        if isinstance(value, list):
            return [self.substitute(item, scope, (*location, index)) for index, item in enumerate(value)]
        if not isinstance(value, str):
            return value
        if value.startswith('*') and value[1:] in self.anchor_lines:
            anchor = f'&{value[1:]} on line {self.anchor_lines[value[1:]]}'
            message = f'{value} is quoted, so YAML reads it as text. Write {value} unquoted, to repeat the value anchored as {anchor}'
            self.fault('quoted_alias', message, location, value)
            return value
        used: list[str] = []

        def replace(match: re.Match[str]) -> str:
            if match['escaped'] is not None:
                return f'{{{match["escaped"]}}}'
            name = match['name']
            if name == RESERVED_NAME:
                return match[0]
            if name in scope:
                used.append(f'{match[0]} from {scope[name].origin}')
                used.extend(scope[name].used)
                return scope[name].value
            reason = self.unreachable(match[0], name, scope, below, location, whole=match[0] == value)
            self.fault('undefined_variable', reason, location, value)
            return match[0]

        substituted = VARIABLE_TOKEN.sub(replace, value)
        if substituted != value:
            self.changed[location] = (substituted, Written(value, tuple(used)))
        return substituted

    def unreachable(self, token: str, name: str, scope: dict[str, Variable], below: frozenset[str], location: Location, whole: bool) -> str:
        """Why `token` resolves to nothing here, most likely cause first.

        The escape is offered last, because escaping a variable that exists elsewhere ships the
        braces to AWS as text. Moving a variable to the top level is offered only where it would
        work: one built from another pipeline's variables cannot be read outside that pipeline.
        An anchor of the same name is offered only as `*name`, and only where `whole` says the
        token is the entire value, because an alias cannot sit inside a longer string."""
        if name in below:
            return f'variable {token} is defined below this one, and a variable sees only those above it'
        if name in self.defined:
            where = ' and '.join(definition.origin for definition in self.defined[name])
            unseen = f'variable {token} is defined in {where}, which this value cannot see'
            if location[0] != 'pipelines' or len(location) < 2:
                return f"{unseen}. A pipeline's variables reach that pipeline alone; move it to the top-level variables block"
            here = f'pipelines.{location[1]}.variables'
            built_from = self.pipeline_variables_in(name)
            if built_from:
                return f'{unseen}. It is built from the pipeline variables {built_from}, so define {token} in {here} too'
            return (
                f"{unseen}. A pipeline's variables reach that pipeline alone: define it in {here} too, or move it "
                'to the top-level variables block if every pipeline shares its value'
            )
        missing = f'variable {token} cannot be found'
        if name in self.anchor_lines:
            anchor = f'&{name} on line {self.anchor_lines[name]}'
            if not whole:
                return (
                    f'{missing}. {anchor} is a YAML anchor, which repeats a whole value and cannot go inside one. '
                    f'Define {name} under variables to use it here'
                )
            key = f'{location[-1]}: ' if isinstance(location[-1], str) else ''
            return f'{missing}. {anchor} is a YAML anchor, not a variable. Repeat it as the whole value, unquoted: {key}*{name}'
        escape = f'If a job or Jenkinsfile fills {token} itself, write {{{token}}}'
        # A placeholder the job fills can sit close to a variable's name, as {date} does to {dataset}.
        close = difflib.get_close_matches(name, list(scope), n=1)
        if close:
            return f'{missing}. Did you mean {{{close[0]}}}? {escape}'
        return f'{missing}. {escape}'

    def pipeline_variables_in(self, name: str) -> str:
        """The `{name}`s a definition of `name` uses that no top-level variable supplies, joined for a message."""
        top_level = {other for other, definitions in self.defined.items() if any(d.origin == 'variables' for d in definitions)}
        found: list[str] = []
        for definition in self.defined[name]:
            if isinstance(definition.written, str):
                for match in VARIABLE_TOKEN.finditer(definition.written):
                    used = match['name']
                    if used and used != RESERVED_NAME and used not in top_level and f'{{{used}}}' not in found:
                        found.append(f'{{{used}}}')
        return ' and '.join(found)

    def unquoted(self, value: dict[str, None], scope: dict[str, Variable], location: Location) -> None:
        (name,) = value
        message = f'{{{name}}} was read as a YAML mapping, because the value starts with {{; quote it'
        if name not in scope and name != RESERVED_NAME:
            message += f'. Quoted, {self.unreachable(f"{{{name}}}", name, scope, frozenset(), location, whole=True)}'
        self.fault('unquoted_variable', message, location, value)

    def resolve_block(self, block: Any, inherited: dict[str, Variable], location: Location) -> tuple[Any, dict[str, Variable]]:
        """The block with each value resolved against those above it, and the scope it leaves.

        In file order, so a variable sees the inherited ones and its own block's earlier entries,
        which is what keeps a reference cycle unwritable."""
        if not isinstance(block, dict):
            return block, inherited
        scope = dict(inherited)
        resolved: dict[Any, Any] = {}
        names = list(block)
        for position, (name, value) in enumerate(block.items()):
            here = (*location, name)
            # Defined as itself whenever it is refused, so its references report nothing more.
            placeholder = Variable(f'{{{name}}}', dotted(here))
            if not isinstance(name, str) or not NAME.fullmatch(name):
                self.fault(
                    'invalid_variable_name',
                    f'{name!r} is not a name {{name}} can reference: a letter or _, then letters, digits, _ or -',
                    here,
                    value,
                )
            elif name == RESERVED_NAME:
                self.fault('reserved_variable', f'{ENV_PLACEHOLDER} is the environment and cannot be a variable', here, value)
            elif name in inherited:
                self.fault('shadowed_variable', f'{{{name}}} is a top-level variable; a pipeline cannot redefine it', here, value)
            elif unquoted_token(value):
                self.unquoted(value, scope, here)
                scope[name] = placeholder
            elif not isinstance(value, str):
                self.fault('variable_not_a_string', not_a_string(value), here, value)
                scope[name] = placeholder
            else:
                substituted = self.substitute(value, scope, here, frozenset(names[position + 1 :]))
                built_from = self.changed[here][1].variables if here in self.changed else ()
                scope[name] = Variable(substituted, dotted(here), built_from)
            resolved[name] = scope[name].value if name in scope and name not in inherited else value
        return resolved, scope


def not_a_string(value: Any) -> str:
    """Why a variable's value is refused, in terms of what YAML made of it."""
    if isinstance(value, dict | list):
        shape = 'mapping' if isinstance(value, dict) else 'list'
        return (
            f'a variable is one string, and YAML read this as a {shape}. To reuse a block, anchor it where it '
            'is first used, as `load: &job`, then repeat it with `*job` or merge it with `<<: *job`'
        )
    if value is None:
        return 'a variable is a string, and this one is empty'
    return 'a variable is a string; quote it, since YAML reads an unquoted 0123 as octal'


def unquoted_token(value: Any) -> bool:
    """Whether YAML read an unquoted `{name}` as the one-key mapping `{name: None}`."""
    if not isinstance(value, dict) or len(value) != 1:
        return False
    ((key, item),) = value.items()
    return item is None and isinstance(key, str) and NAME.fullmatch(key) is not None


def defined_variables(raw: dict[Any, Any]) -> dict[str, list[Definition]]:
    """Every variable name the file defines, at either level, and each block defining it."""
    blocks: list[tuple[str, Any]] = [('variables', raw.get('variables'))]
    pipelines = raw.get('pipelines')
    if isinstance(pipelines, dict):
        blocks += [(f'pipelines.{name}.variables', p.get('variables')) for name, p in pipelines.items() if isinstance(p, dict)]
    defined: dict[str, list[Definition]] = {}
    for origin, block in blocks:
        if isinstance(block, dict):
            for name, written in block.items():
                defined.setdefault(str(name), []).append(Definition(origin, written))
    return defined


def expand_variables(raw: Any, anchor_lines: dict[str, int]) -> Expansion:
    """The parsed config with every variable substituted, or a `ValidationError` naming each fault."""
    if not isinstance(raw, dict):
        return Expansion(raw, {})
    run = Pass(defined_variables(raw), anchor_lines)
    top_block, top_scope = run.resolve_block(raw.get('variables'), {}, ('variables',))
    expanded: dict[Any, Any] = {}
    for key, value in raw.items():
        if key == 'variables':
            expanded[key] = top_block
        elif key == 'pipelines' and isinstance(value, dict):
            expanded[key] = {name: expand_pipeline(run, name, pipeline, top_scope) for name, pipeline in value.items()}
        else:
            expanded[key] = run.substitute(value, top_scope, (key,))
    if run.faults:
        raise ValidationError.from_exception_data('DectlConfig', run.faults)
    writings: dict[str, dict[Location, tuple[str, Written]]] = {}
    for location, change in run.changed.items():
        if location[0] == 'pipelines' and len(location) > 2:
            writings.setdefault(str(location[1]), {})[location[2:]] = change
    return Expansion(expanded, writings)


def expand_pipeline(run: Pass, name: str, pipeline: Any, top_scope: dict[str, Variable]) -> Any:
    if not isinstance(pipeline, dict):
        return pipeline
    location: Location = ('pipelines', name)
    block, scope = run.resolve_block(pipeline.get('variables'), top_scope, (*location, 'variables'))
    return {key: block if key == 'variables' else run.substitute(value, scope, (*location, key)) for key, value in pipeline.items()}
