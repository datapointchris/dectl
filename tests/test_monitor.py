from dectl.commands.monitor import build_monitor_sources
from dectl.config import DectlConfig
from dectl.env import active_environment


def pipeline_from(pipeline_config: dict):
    config = DectlConfig.model_validate(
        {
            'defaults': {'account_id': '111111111111'},
            'pipelines': {'p': pipeline_config},
        }
    )
    return config.pipelines['p']


def test_build_monitor_sources_resolves_configured_resources():
    pipeline = pipeline_from(
        {
            'lambdas': {'ingest': {'name': 'my-ingest', 'source_dir': 'x'}},
            'step_functions': {'flow': {'name': 'my-flow', 'log_group': '/aws/vendedlogs/states/my-flow'}},
            'monitor': {'lambdas': ['ingest'], 'step_functions': ['flow']},
        }
    )
    sources, warnings = build_monitor_sources(pipeline)
    groups = [source[1] for source in sources]

    assert warnings == []
    assert '/aws/lambda/my-ingest' in groups
    assert '/aws/vendedlogs/states/my-flow' in groups


def test_build_monitor_sources_warns_when_step_function_has_no_log_group():
    pipeline = pipeline_from(
        {
            'step_functions': {'flow': {'name': 'my-flow'}},
            'monitor': {'step_functions': ['flow']},
        }
    )
    sources, warnings = build_monitor_sources(pipeline)

    assert sources == []
    assert any('log_group' in warning for warning in warnings)


def test_build_monitor_sources_warns_on_unconfigured_resource():
    pipeline = pipeline_from({'monitor': {'lambdas': ['ghost']}})
    sources, warnings = build_monitor_sources(pipeline)

    assert sources == []
    assert any('ghost' in warning for warning in warnings)


def test_build_monitor_sources_substitutes_active_env(monkeypatch):
    monkeypatch.setattr(active_environment, 'name', 'prod')
    pipeline = pipeline_from(
        {
            'lambdas': {'ingest': {'name': 'salesdata-{env}-ingest', 'source_dir': 'x'}},
            'monitor': {'lambdas': ['ingest']},
        }
    )
    sources, warnings = build_monitor_sources(pipeline)

    assert warnings == []
    assert sources[0][1] == '/aws/lambda/salesdata-prod-ingest'


def test_build_monitor_sources_pads_aliases_to_align():
    pipeline = pipeline_from(
        {
            'lambdas': {
                'a': {'name': 'fn-a', 'source_dir': 'x'},
                'longer-name': {'name': 'fn-b', 'source_dir': 'x'},
            },
            'monitor': {'lambdas': ['a', 'longer-name']},
        }
    )
    sources, warnings = build_monitor_sources(pipeline)
    prefixes = [source[0] for source in sources]

    assert warnings == []
    # 'a' is padded to the width of 'longer-name' so the columns line up.
    assert 'a          ' in prefixes[0]
