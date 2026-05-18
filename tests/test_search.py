from dectl.commands.search import SERVICES


def test_services_list_is_not_empty():
    assert len(SERVICES) > 0


def test_services_contains_expected():
    assert 's3' in SERVICES
    assert 'lambda' in SERVICES
    assert 'glue' in SERVICES
