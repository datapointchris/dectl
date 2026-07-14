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
from dectl.output import error
from dectl.output import info
from dectl.output import success


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


def make_lambda_app(pipeline_name: str, pipeline, config: DectlConfig) -> typer.Typer:
    lambdas = pipeline.lambdas
    alias_list = ', '.join(lambdas.keys()) or '(none configured)'
    example = next(iter(lambdas), 'FUNCTION')
    function_arg_help = f'Function alias from config. Available: {alias_list}'

    lambda_app = typer.Typer(
        no_args_is_help=True,
        help=(f'Deploy, invoke, and tail Lambda functions in [bold]{pipeline_name}[/bold].\n\nFunctions: {alias_list}'),
    )

    def resolve_function(function: str) -> LambdaConfig:
        if function not in lambdas:
            known = ', '.join(lambdas.keys())
            error(f'unknown function "{function}" for pipeline {pipeline_name}. known: {known}')
            raise typer.Exit(1)
        return render_env_model(lambdas[function])

    @lambda_app.command(
        epilog=(
            'Examples:\n\n'
            f'dectl {pipeline_name} lambda deploy {example} — update $LATEST only (test with invoke)\n\n'
            f'dectl {pipeline_name} lambda deploy {example} --publish — publish a version and move the alias so triggers run it'
        ),
    )
    def deploy(
        function: Annotated[str, typer.Argument(help=function_arg_help)],
        publish: Annotated[
            bool,
            typer.Option(
                '--publish',
                '-p',
                help='Publish an immutable version and move the configured alias so alias/trigger invocations run the new code',
            ),
        ] = False,
    ) -> None:
        """Zip and deploy a Lambda function.

        Without --publish, only $LATEST is updated -- test it directly with
        `invoke`, which targets $LATEST. With --publish, a new immutable
        version is published and the function's configured alias is repointed
        to it, so alias-following invocations (S3 triggers, durable functions)
        pick up the new code. Editing $LATEST alone never changes a published
        version, which is why an alias-triggered function keeps running old code.
        """
        from dectl.session import make_session

        fn_config = resolve_function(function)
        session = make_session(config)
        client = session.client('lambda')

        info(f'zipping {fn_config.source_dir}')
        zip_path = zip_lambda(fn_config.source_dir)

        info(f'updating code for {fn_config.name}')
        with zip_path.open('rb') as f:
            client.update_function_code(
                FunctionName=fn_config.name,
                ZipFile=f.read(),
            )
        zip_path.unlink()

        if not publish:
            success(f'deployed {fn_config.name} ($LATEST)')
            return

        info('waiting for code update to settle')
        client.get_waiter('function_updated_v2').wait(FunctionName=fn_config.name)

        version = client.publish_version(FunctionName=fn_config.name)['Version']
        info(f'published version {version}')

        if not fn_config.alias:
            success(f'deployed {fn_config.name} version {version} (no alias configured to move)')
            return

        client.update_alias(
            FunctionName=fn_config.name,
            Name=fn_config.alias,
            FunctionVersion=version,
        )
        success(f'deployed {fn_config.name}: alias {fn_config.alias} -> version {version}')

    @lambda_app.command(
        epilog=(
            'Examples:\n\n'
            f'dectl {pipeline_name} lambda invoke {example}\n\n'
            f"""dectl {pipeline_name} lambda invoke {example} '{{"key": "value"}}'"""
        ),
    )
    def invoke(
        function: Annotated[str, typer.Argument(help=function_arg_help)],
        payload: Annotated[str, typer.Argument(help='JSON event payload to send. Targets $LATEST.')] = '{}',
    ) -> None:
        """Invoke a Lambda function synchronously ($LATEST) and print the JSON response.

        This calls the unqualified function, so it runs the latest deployed code
        even if you have not published a version yet — ideal for the fast dev loop.
        """
        from dectl.session import make_session

        fn_config = resolve_function(function)
        session = make_session(config)
        client = session.client('lambda')

        resp = client.invoke(
            FunctionName=fn_config.name,
            Payload=payload.encode(),
        )
        result = json.loads(resp['Payload'].read())
        # A handled/unhandled exception in the function still returns 200 with the error
        # payload; FunctionError is the only signal it failed. Without this the stack trace
        # prints as though it were a successful result.
        if resp.get('FunctionError'):
            error(f'function returned an error ({resp["FunctionError"]}):')
            console.print_json(json.dumps(result, indent=2))
            raise typer.Exit(1)
        console.print_json(json.dumps(result, indent=2))

    @lambda_app.command(
        epilog=f'Example:\n\ndectl {pipeline_name} lambda logs {example}',
    )
    def logs(
        function: Annotated[str, typer.Argument(help=function_arg_help)],
    ) -> None:
        """Tail CloudWatch logs for a Lambda function's most recent log stream (Ctrl-C to stop)."""
        from dectl.session import make_session

        fn_config = resolve_function(function)
        session = make_session(config)
        logs_client = session.client('logs')
        tail_lambda_logs(logs_client, fn_config.name, follow=True)

    return lambda_app
