from typer.testing import CliRunner

from dectl.commands.s3 import make_s3_app
from dectl.commands.s3 import shell_variable_name
from dectl.config import DectlConfig
from dectl.env import active_environment

runner = CliRunner()


def make_config(buckets: dict[str, str]) -> DectlConfig:
    return DectlConfig.model_validate(
        {
            'defaults': {'account_id': '111111111111'},
            'pipelines': {'proj': {'buckets': buckets}},
        }
    )


def test_shell_variable_name_is_lowercase_with_underscores():
    assert shell_variable_name('my-proj', 'raw-data') == 'my_proj_raw_data'


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


def test_unknown_bucket_is_not_a_command():
    # Buckets are sub-apps now, so an unknown bucket is an unknown command (Typer usage error).
    config = make_config({'raw': 'my-raw-bucket'})
    app = make_s3_app('proj', config.pipelines['proj'], config)

    result = runner.invoke(app, ['nope', 'mount'])

    assert result.exit_code != 0


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
