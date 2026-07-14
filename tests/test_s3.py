from typer.testing import CliRunner

from dectl.commands.s3 import make_s3_app
from dectl.commands.s3 import shell_variable_name
from dectl.config import DectlConfig

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


def test_mount_rejects_unknown_shortname():
    config = make_config({'raw': 'my-raw-bucket'})
    app = make_s3_app('proj', config.pipelines['proj'], config)

    result = runner.invoke(app, ['mount', 'nope'])

    assert result.exit_code == 1
    assert 'unknown bucket' in result.stdout
