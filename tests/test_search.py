import json
from types import SimpleNamespace

from dectl.commands import search as search_mod


def test_run_search_json_emits_only_matching_services(monkeypatch, capsys):
    # Stub the per-service scan so the test exercises the JSON assembly, not boto3.
    monkeypatch.setattr(
        search_mod,
        'search_service',
        lambda client, keyword, service: [[f'{keyword}-{service}']] if service == 's3' else [],
    )

    search_mod.run_search(session=SimpleNamespace(client=lambda *_args, **_kwargs: None), keyword='found', region='us-east-2', as_json=True)

    data = json.loads(capsys.readouterr().out)
    assert data['keyword'] == 'found'
    assert data['region'] == 'us-east-2'
    assert data['matches'] == {'s3': [['found-s3']]}  # empty services are dropped
