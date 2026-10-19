"""The config's `variables` blocks, substituted into every string value before the models validate.

A variable is static, so it is replaced once, at load, in the raw mapping YAML parsed. `{env}` is
not: `--env` is parsed after the command tree is built, so it passes through untouched for
`env.substitute_env` to replace at each command. The top-level block reaches the whole file, and
a pipeline's block reaches that pipeline alone.

Nothing a variable could replace is overwritten. `env` cannot be a variable's name, a pipeline
cannot redefine a top-level variable, and a `{name}` naming no variable is refused rather than
left in place to reach AWS as a literal name. `{{name}}` is the escape, and renders as `{name}`.
Mapping keys are aliases and are never substituted.

Every string a substitution changed is recorded with what the file wrote, so a fault found later
in the substituted value can quote the line the reader has to edit.
"""

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


class Expansion(NamedTuple):
    config: Any
    # Pipeline name -> location within that pipeline -> (substituted value, what the file wrote).
    writings: dict[str, dict[Location, tuple[str, Written]]]


def dotted(location: Location) -> str:
    return '.'.join(str(part) for part in location)


class Pass:
    """One expansion of one file: the faults it found and the strings it changed."""

    def __init__(self) -> None:
        self.faults: list[InitErrorDetails] = []
        self.changed: dict[Location, tuple[str, Written]] = {}

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
            return {key: self.substitute(item, scope, (*location, key)) for key, item in value.items()}
        if isinstance(value, list):
            return [self.substitute(item, scope, (*location, index)) for index, item in enumerate(value)]
        if not isinstance(value, str):
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
            if name in below:
                message = f'{match[0]} is defined below this variable, and a variable sees only those above it'
            else:
                message = f'{match[0]} names no variable; write {{{match[0]}}} for a literal {match[0]}'
            self.fault('undefined_variable', message, location, value)
            return match[0]

        substituted = VARIABLE_TOKEN.sub(replace, value)
        if substituted != value:
            self.changed[location] = (substituted, Written(value, tuple(used)))
        return substituted

    def unquoted(self, value: dict[str, None], scope: dict[str, Variable], location: Location) -> None:
        (name,) = value
        message = f'{{{name}}} was read as a YAML mapping, because the value starts with {{; quote it'
        if name not in scope and name != RESERVED_NAME:
            message += f'; {{{name}}} also names no variable'
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
                self.fault(
                    'variable_not_a_string', 'a variable is a string; quote it, since YAML reads an unquoted 0123 as octal', here, value
                )
                scope[name] = placeholder
            else:
                substituted = self.substitute(value, scope, here, frozenset(names[position + 1 :]))
                built_from = self.changed[here][1].variables if here in self.changed else ()
                scope[name] = Variable(substituted, dotted(here), built_from)
            resolved[name] = scope[name].value if name in scope and name not in inherited else value
        return resolved, scope


def unquoted_token(value: Any) -> bool:
    """Whether YAML read an unquoted `{name}` as the one-key mapping `{name: None}`."""
    if not isinstance(value, dict) or len(value) != 1:
        return False
    ((key, item),) = value.items()
    return item is None and isinstance(key, str) and NAME.fullmatch(key) is not None


def expand_variables(raw: Any) -> Expansion:
    """The parsed config with every variable substituted, or a `ValidationError` naming each fault."""
    if not isinstance(raw, dict):
        return Expansion(raw, {})
    run = Pass()
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
