import importlib.metadata

import boto3
from botocore.config import Config

from dectl.config import DectlConfig


def installed_version() -> str:
    try:
        return importlib.metadata.version('dectl')
    except importlib.metadata.PackageNotFoundError:
        return 'unknown'


# The settings every AWS client dectl builds carries. Standard mode retries throttling and
# transient errors with backoff, which legacy mode does not do evenly across services. The user
# agent marks dectl's calls in CloudTrail. A caller's own Config is merged over this one, and its
# explicit options win, which is how the Lambda Invoke client keeps exactly one attempt.
DEFAULT_CLIENT_CONFIG = Config(
    retries={'mode': 'standard'},
    user_agent_extra=f'dectl/{installed_version()}',
)


class SharedSettingsSession(boto3.Session):
    """A boto3 session whose every client starts from `DEFAULT_CLIENT_CONFIG`."""

    def client(self, *args, config: Config | None = None, **kwargs):
        merged = DEFAULT_CLIENT_CONFIG if config is None else DEFAULT_CLIENT_CONFIG.merge(config)
        return super().client(*args, config=merged, **kwargs)


def make_session(config: DectlConfig) -> boto3.Session:
    kwargs: dict = {'region_name': config.defaults.region}
    if config.defaults.aws_profile:
        kwargs['profile_name'] = config.defaults.aws_profile
    return SharedSettingsSession(**kwargs)
