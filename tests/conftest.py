import pytest


def pytest_addoption(parser):
    parser.addoption(
        '--run-integration',
        action='store_true',
        default=False,
        help='Run live AWS integration tests. These create and delete real AWS resources and require credentials.',
    )


def pytest_collection_modifyitems(config, items):
    if config.getoption('--run-integration'):
        return
    skip_integration = pytest.mark.skip(reason='live AWS test; pass --run-integration to run')
    for item in items:
        if 'integration' in item.keywords:
            item.add_marker(skip_integration)
