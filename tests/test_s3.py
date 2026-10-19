import pytest

from dectl.commands.s3 import make_s3_app
from dectl.config import DectlConfig
from dectl.env import active_environment
from tests.conftest import RefusalRunner

runner = RefusalRunner()


def make_config(buckets: dict[str, str]) -> DectlConfig:
    return DectlConfig.model_validate(
        {
            'defaults': {'account_id': '111111111111'},
            'pipelines': {'proj': {'buckets': buckets}},
        }
    )


def test_export_emits_evalable_lowercase_statements():
    config = make_config({'raw': 'my-raw-bucket', 'curated': 'my-curated-bucket'})
    app = make_s3_app('my-proj', config.pipelines['proj'], config)

    result = runner.invoke(app, ['export'])

    assert result.exit_code == 0
    assert "export my_proj_raw='s3://my-raw-bucket'" in result.stdout
    assert "export my_proj_curated='s3://my-curated-bucket'" in result.stdout


def test_export_substitutes_active_env_in_bucket_name(monkeypatch):
    monkeypatch.setattr(active_environment, 'name', 'prod')
    config = make_config({'raw': 'salesdata-{env}-raw'})
    app = make_s3_app('proj', config.pipelines['proj'], config)

    result = runner.invoke(app, ['export'])

    assert result.exit_code == 0
    assert "export proj_raw='s3://salesdata-prod-raw'" in result.stdout


def test_uri_prints_bare_s3_uri(monkeypatch):
    monkeypatch.setattr(active_environment, 'name', 'prod')
    config = make_config({'raw': 'salesdata-{env}-raw'})
    app = make_s3_app('proj', config.pipelines['proj'], config)

    result = runner.invoke(app, ['raw', 'uri'])

    assert result.exit_code == 0
    # Bare URI, no markup, so it composes in $(...).
    assert result.stdout.strip() == 's3://salesdata-prod-raw'


def test_export_prefix_overrides_variable_name():
    config = make_config({'raw': 'my-raw-bucket'})
    app = make_s3_app('my-proj', config.pipelines['proj'], config)

    result = runner.invoke(app, ['export', '--prefix', 'ds'])

    assert result.exit_code == 0
    assert "export ds_raw='s3://my-raw-bucket'" in result.stdout


@pytest.mark.parametrize('argv', [['export'], ['raw', 'uri'], ['raw', 'mount']])
def test_every_verb_that_hands_a_bucket_name_out_refuses_one_s3_cannot_address(argv):
    # `config validate` refused this name and no verb acting on it did. `s3 export` is
    # documented for `eval "$(...)"`, so the name landed in the caller's shell; `mount` passed
    # it to mount-s3, whose message names neither the config key nor the pipeline.
    config = make_config({'raw': 'My_Bad_Bucket'})
    app = make_s3_app('proj', config.pipelines['proj'], config)

    result = runner.invoke(app, argv)

    assert result.exit_code == 1
    # Nothing on stdout, so a caller evaluating it evaluates nothing rather than a broken
    # assignment carrying a name that can never resolve.
    assert result.stdout == ''


def test_export_refuses_before_printing_any_of_the_assignments():
    # One bad name among good ones takes the whole run, rather than leaving the caller's shell
    # holding the buckets that came before it in iteration order.
    config = make_config({'raw': 'fine-raw-bucket', 'bad': 'My_Bad_Bucket', 'curated': 'fine-curated'})
    app = make_s3_app('proj', config.pipelines['proj'], config)

    result = runner.invoke(app, ['export'])

    assert result.exit_code == 1
    assert result.stdout == ''
