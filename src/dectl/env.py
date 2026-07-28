from typing import Any

from pydantic import BaseModel

from dectl.output import warn

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
        self.warned_about_missing_placeholder = False


active_environment = ActiveEnvironment()

# Sources that mean the user deliberately asked for an environment, so an env that changes nothing
# is a mistake worth reporting. A config default or the built-in 'dev' carries no such intent.
EXPLICIT_ENV_SOURCES = frozenset({'--env', 'DECTL_ENV'})


def set_active_environment(name: str, source: str = 'default') -> None:
    active_environment.name = name
    active_environment.source = source
    active_environment.warned_about_missing_placeholder = False


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


def contains_env_placeholder(value: Any) -> bool:
    if isinstance(value, str):
        return ENV_PLACEHOLDER in value
    if isinstance(value, dict):
        return any(contains_env_placeholder(item) for item in value.values())
    if isinstance(value, list):
        return any(contains_env_placeholder(item) for item in value)
    return False


def warn_if_environment_had_no_effect(value: Any) -> None:
    """Report an explicitly-requested env that substitutes nothing.

    Substitution is a literal {env} replacement, so a resource whose config hardcodes its
    environment ignores --env / DECTL_ENV entirely and quietly acts on whatever env its names
    spell out. That failure is invisible — the command succeeds against the wrong environment —
    so say it out loud. Warned once per invocation, since every verb resolves its config here."""
    if active_environment.source not in EXPLICIT_ENV_SOURCES:
        return
    if contains_env_placeholder(value) or active_environment.warned_about_missing_placeholder:
        return
    active_environment.warned_about_missing_placeholder = True
    warn(
        f"{active_environment.source}={active_environment.name} changed nothing: this resource's config has no "
        f'{ENV_PLACEHOLDER} placeholder, so its AWS names are used exactly as written and the environment you '
        'asked for is ignored. Run "dectl PIPELINE list" to see the names being targeted.'
    )


def render_env_model[ModelT: BaseModel](model: ModelT) -> ModelT:
    """Return a copy of a config model with {env} substituted in every string field."""
    data = model.model_dump()
    warn_if_environment_had_no_effect(data)
    return type(model).model_validate(render_value(data))
