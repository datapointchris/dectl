from typing import Any

from pydantic import BaseModel

DEFAULT_ENV = 'dev'
ENV_PLACEHOLDER = '{env}'


class ActiveEnvironment:
    """Holds the env resolved from --env / DECTL_ENV for one CLI invocation.

    Resource names in config carry an {env} placeholder (e.g. salesdata-{env}-ds-thing). The active
    env is substituted in at the point each command resolves its config, so one config drives
    dev/staging/prod by swapping a single token rather than duplicating every name per env.

    `source` records where the value came from (--env / DECTL_ENV / config / default) so the CLI
    can tell you not just which environment you are in but why."""

    def __init__(self) -> None:
        self.name = DEFAULT_ENV
        self.source = 'default'


active_environment = ActiveEnvironment()


def set_active_environment(name: str, source: str = 'default') -> None:
    active_environment.name = name
    active_environment.source = source


def describe_active_environment() -> str:
    return f'{active_environment.name} (from {active_environment.source})'


def substitute_env(value: str) -> str:
    # Replace only the literal {env} token, leaving any other braces (JSON payloads, IAM policy
    # variables) untouched — which str.format would not.
    return value.replace(ENV_PLACEHOLDER, active_environment.name)


def render_value(value: Any) -> Any:
    if isinstance(value, str):
        return substitute_env(value)
    if isinstance(value, dict):
        return {key: render_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [render_value(item) for item in value]
    return value


def render_env_model[ModelT: BaseModel](model: ModelT) -> ModelT:
    """Return a copy of a config model with {env} substituted in every string field."""
    return type(model).model_validate(render_value(model.model_dump()))
