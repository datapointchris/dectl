import importlib.metadata
from typing import TYPE_CHECKING

from dectl.config import DectlConfig

# boto3 is imported where a session is built rather than here. Every command imports this module
# to reach the door, and most never open it: `reference`, `env`, `list`, `config`, `--help` and
# shell completion build no AWS client, and boto3 is about a quarter of a start without it.
if TYPE_CHECKING:
    import boto3
    from botocore.config import Config


def installed_version() -> str:
    try:
        return importlib.metadata.version('dectl')
    except importlib.metadata.PackageNotFoundError:
        return 'unknown'


def default_client_config() -> 'Config':
    """The settings every AWS client dectl builds carries.

    Standard mode retries throttling and transient errors with backoff, which legacy mode does
    not do evenly across services. The user agent marks dectl's calls in CloudTrail. botocore
    merges a caller's own Config over this one, and its explicit options win, which is how the
    Lambda Invoke client keeps exactly one attempt."""
    from botocore.config import Config

    return Config(retries={'mode': 'standard'}, user_agent_extra=f'dectl/{installed_version()}')


def make_session(config: DectlConfig) -> 'boto3.Session':
    import boto3
    import botocore.session

    core = botocore.session.get_session()
    core.set_default_client_config(default_client_config())
    kwargs: dict = {'botocore_session': core, 'region_name': config.defaults.region}
    if config.defaults.aws_profile:
        kwargs['profile_name'] = config.defaults.aws_profile
    return boto3.Session(**kwargs)
