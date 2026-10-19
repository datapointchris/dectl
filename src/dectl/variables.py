"""The config's `variables` blocks, substituted into every string value before the models validate.

A variable is static, so it is replaced once, at load, in the raw mapping YAML parsed. `{env}` is
not: `--env` is parsed after the command tree is built, so it passes through untouched for
`env.substitute_env` to replace at each command. The top-level block reaches the whole file, and
a pipeline's block reaches that pipeline alone.

Nothing a variable could replace is overwritten. `env` cannot be a variable's name, a pipeline
cannot redefine a top-level variable, and a `{name}` naming no variable is refused rather than
left in place to reach AWS as a literal name. Mapping keys are aliases and are never substituted.
"""

import re
from typing import Any

from pydantic import ValidationError
from pydantic_core import InitErrorDetails
from pydantic_core import PydanticCustomError

from dectl.values import ENV_PLACEHOLDER

# A `$` before the brace makes it a Terraform or environment-variable placeholder, never a variable.
VARIABLE_TOKEN = re.compile(r'(?<!\$)\{([A-Za-z_][A-Za-z0-9_-]*)\}')
RESERVED_NAME = ENV_PLACEHOLDER.strip('{}')

type Location = tuple[str | int, ...]


def fault(kind: str, message: str, location: Location, value: Any) -> InitErrorDetails:
    return InitErrorDetails(type=PydanticCustomError(kind, message), loc=location, input=value)


def substitute(value: Any, scope: dict[str, str], location: Location, faults: list[InitErrorDetails]) -> Any:
    """`value` with every `{name}` in its strings replaced from `scope`, at any depth."""
    if isinstance(value, dict):
        return {key: substitute(item, scope, (*location, key), faults) for key, item in value.items()}
    if isinstance(value, list):
        return [substitute(item, scope, (*location, index), faults) for index, item in enumerate(value)]
    if not isinstance(value, str):
        return value

    def replace(match: re.Match[str]) -> str:
        name = match[1]
        if name == RESERVED_NAME:
            return match[0]
        if name in scope:
            return scope[name]
        message = f'{match[0]} names no variable; a variable sees only those defined above it'
        faults.append(fault('undefined_variable', message, location, value))
        return match[0]

    return VARIABLE_TOKEN.sub(replace, value)


def resolve_block(block: Any, inherited: dict[str, str], location: Location, faults: list[InitErrorDetails]) -> tuple[Any, dict[str, str]]:
    """The block with each value resolved against those above it, and the scope it leaves.

    In file order, so a variable sees the inherited ones and its own block's earlier entries,
    which is what keeps a reference cycle unwritable."""
    if not isinstance(block, dict):
        return block, inherited
    scope = dict(inherited)
    resolved: dict[Any, Any] = {}
    for name, value in block.items():
        here = (*location, name)
        if name == RESERVED_NAME:
            faults.append(fault('reserved_variable', f'{ENV_PLACEHOLDER} is the environment and cannot be a variable', here, value))
        elif name in inherited:
            faults.append(fault('shadowed_variable', f'{{{name}}} is a top-level variable; a pipeline cannot redefine it', here, value))
        elif not isinstance(value, str):
            message = 'a variable is a string; quote it, since YAML reads an unquoted 0123 as octal'
            faults.append(fault('variable_not_a_string', message, here, value))
            # Defined as itself, so the references to it report nothing beyond the one fault.
            scope[name] = f'{{{name}}}'
        else:
            scope[name] = substitute(value, scope, here, faults)
        resolved[name] = scope.get(name, value)
    return resolved, scope


def expand_variables(raw: Any) -> Any:
    """The parsed config with every variable substituted, or a `ValidationError` naming each fault."""
    if not isinstance(raw, dict):
        return raw
    faults: list[InitErrorDetails] = []
    top_block, top_scope = resolve_block(raw.get('variables'), {}, ('variables',), faults)
    expanded: dict[Any, Any] = {}
    for key, value in raw.items():
        if key == 'variables':
            expanded[key] = top_block
        elif key == 'pipelines' and isinstance(value, dict):
            expanded[key] = {name: expand_pipeline(name, pipeline, top_scope, faults) for name, pipeline in value.items()}
        else:
            expanded[key] = substitute(value, top_scope, (key,), faults)
    if faults:
        raise ValidationError.from_exception_data('DectlConfig', faults)
    return expanded


def expand_pipeline(name: str, pipeline: Any, top_scope: dict[str, str], faults: list[InitErrorDetails]) -> Any:
    if not isinstance(pipeline, dict):
        return pipeline
    location: Location = ('pipelines', name)
    block, scope = resolve_block(pipeline.get('variables'), top_scope, (*location, 'variables'), faults)
    return {key: block if key == 'variables' else substitute(value, scope, (*location, key), faults) for key, value in pipeline.items()}
