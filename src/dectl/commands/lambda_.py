import json
import tempfile
import zipfile
from pathlib import Path
from typing import Annotated

import typer

from dectl.config import DectlConfig
from dectl.config import LambdaConfig
from dectl.env import render_env_model
from dectl.logs import tail_lambda_logs
from dectl.output import console
from dectl.output import emit_json
from dectl.output import error
from dectl.output import info
from dectl.output import success
from dectl.payloads import read_payload


def zip_lambda(source_dir: str) -> Path:
    source = Path(source_dir)
    if not source.exists():
        error(f'source directory not found: {source_dir}')
        raise typer.Exit(1)

    zip_path = Path(tempfile.mkdtemp()) / 'lambda.zip'
    with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zf:
        for file in source.rglob('*'):
            if file.is_file() and '__pycache__' not in file.parts:
                zf.write(file, file.relative_to(source))
    return zip_path


def make_lambda_function_app(pipeline_name: str, alias: str, fn_config: LambdaConfig, config: DectlConfig) -> typer.Typer:
    """Build the per-function sub-app: `dectl PIPELINE lambda <alias> <verb>`.

    Verbs close over this function's config and resolve {env} at call time."""
    fn_app = typer.Typer(
        no_args_is_help=True,
        help=f'Lambda function [bold]{alias}[/bold] → {fn_config.name}',
    )

    def resolved() -> LambdaConfig:
        return render_env_model(fn_config)

    @fn_app.command(
        epilog=(
            'Examples:\n\n'
            f'dectl {pipeline_name} lambda {alias} deploy — update $LATEST only (test with run)\n\n'
            f'dectl {pipeline_name} lambda {alias} deploy --publish — publish a version and move the live alias'
        ),
    )
    def deploy(
        publish: Annotated[
            bool,
            typer.Option(
                '--publish',
                '-p',
                help='Publish an immutable version and move the configured live alias so alias/trigger invocations run it',
            ),
        ] = False,
    ) -> None:
        """Zip and deploy a Lambda function.

        Without --publish, only $LATEST is updated -- test it directly with
        `run`, which targets $LATEST. With --publish, a new immutable version is
        published and the function's configured live alias is repointed to it, so
        alias-following invocations (S3 triggers, durable functions) pick up the
        new code. Editing $LATEST alone never changes a published version, which
        is why an alias-triggered function keeps running old code.
        """
        from dectl.session import make_session

        fn = resolved()
        session = make_session(config)
        client = session.client('lambda')

        info(f'zipping {fn.source_dir}')
        zip_path = zip_lambda(fn.source_dir)

        info(f'updating code for {fn.name}')
        with zip_path.open('rb') as f:
            client.update_function_code(FunctionName=fn.name, ZipFile=f.read())
        zip_path.unlink()

        if not publish:
            success(f'deployed {fn.name} ($LATEST)')
            return

        info('waiting for code update to settle')
        client.get_waiter('function_updated_v2').wait(FunctionName=fn.name)

        version = client.publish_version(FunctionName=fn.name)['Version']
        info(f'published version {version}')

        if not fn.live_alias:
            success(f'deployed {fn.name} version {version} (no live_alias configured to move)')
            return

        client.update_alias(FunctionName=fn.name, Name=fn.live_alias, FunctionVersion=version)
        success(f'deployed {fn.name}: alias {fn.live_alias} -> version {version}')

    @fn_app.command(
        epilog=(
            'Examples:\n\n'
            f'dectl {pipeline_name} lambda {alias} run\n\n'
            f'dectl {pipeline_name} lambda {alias} run --payload-file event.json\n\n'
            f'echo \'{{"key": "value"}}\' | dectl {pipeline_name} lambda {alias} run --payload-file -'
        ),
    )
    def run(
        payload_file: Annotated[
            str | None,
            typer.Option('--payload-file', help='JSON event: a file path, or - for stdin. Defaults to {}.'),
        ] = None,
        as_json: Annotated[bool, typer.Option('--json', help='Emit the response as machine-readable JSON to stdout.')] = False,
    ) -> None:
        """Invoke a Lambda function synchronously ($LATEST) and print the JSON response.

        This calls the unqualified function, so it runs the latest deployed code
        even if you have not published a version yet — ideal for the fast dev loop.
        """
        from dectl.session import make_session

        fn = resolved()
        payload = read_payload(payload_file)
        client = make_session(config).client('lambda')

        resp = client.invoke(FunctionName=fn.name, Payload=payload.encode())
        result = json.loads(resp['Payload'].read())

        # A handled/unhandled exception in the function still returns 200 with the error payload;
        # FunctionError is the only signal it failed. Surface it and exit non-zero so a script
        # (or an LLM) can tell a failed invocation from a successful one.
        if resp.get('FunctionError'):
            if as_json:
                emit_json(result)
            else:
                error(f'function returned an error ({resp["FunctionError"]}):')
                console.print_json(json.dumps(result, indent=2))
            raise typer.Exit(1)

        if as_json:
            emit_json(result)
        else:
            console.print_json(json.dumps(result, indent=2))

    @fn_app.command(epilog=f'Example:\n\ndectl {pipeline_name} lambda {alias} logs --follow')
    def logs(
        follow: Annotated[bool, typer.Option('--follow', '-f', help='Keep tailing new log events (Ctrl-C to stop).')] = False,
    ) -> None:
        """Show CloudWatch logs for a Lambda function's recent activity (add --follow to tail)."""
        from dectl.session import make_session

        fn = resolved()
        logs_client = make_session(config).client('logs')
        tail_lambda_logs(logs_client, fn.name, follow=follow)

    return fn_app


def make_lambda_app(pipeline_name: str, pipeline, config: DectlConfig) -> typer.Typer:
    lambdas = pipeline.lambdas
    alias_list = ', '.join(lambdas.keys()) or '(none configured)'

    lambda_app = typer.Typer(
        no_args_is_help=True,
        help=f'Lambda functions in [bold]{pipeline_name}[/bold] — pick a function, then a verb.\n\nFunctions: {alias_list}',
    )
    for alias, fn_config in lambdas.items():
        lambda_app.add_typer(
            make_lambda_function_app(pipeline_name, alias, fn_config, config),
            name=alias,
            rich_help_panel='Functions',
        )
    return lambda_app
