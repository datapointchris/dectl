import boto3

from dectl.config import DectlConfig


def make_session(config: DectlConfig) -> boto3.Session:
    kwargs: dict = {'region_name': config.defaults.region}
    if config.defaults.aws_profile:
        kwargs['profile_name'] = config.defaults.aws_profile
    return boto3.Session(**kwargs)
