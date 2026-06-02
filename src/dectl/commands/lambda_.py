import json
import tempfile
import zipfile
from pathlib import Path
from typing import Annotated

import typer

from dectl.config import DectlConfig
from dectl.config import LambdaConfig
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
    lambda_app = typer.Typer(help=f'Lambda pipeline: {pipeline_name}')
    lambdas = pipeline.lambdas

    def resolve_function(function: str) -> LambdaConfig:
        if function not in lambdas:
            known = ', '.join(lambdas.keys())
            error(f'unknown function "{function}" for pipeline {pipeline_name}. known: {known}')
            raise typer.Exit(1)
        return lambdas[function]

    @lambda_app.command()
    def deploy(
        function: Annotated[str, typer.Argument(help='Function alias from config')],
    ) -> None:
        """Zip and deploy a Lambda function."""
        from dectl.session import make_session

        fn_config = resolve_function(function)
        session = make_session(config)
        client = session.client('lambda')

        info(f'zipping {fn_config.source_dir}')
        zip_path = zip_lambda(fn_config.source_dir)

        info(f'deploying to {fn_config.name}')
        with zip_path.open('rb') as f:
            client.update_function_code(
                FunctionName=fn_config.name,
                ZipFile=f.read(),
            )
        zip_path.unlink()
        success(f'deployed {fn_config.name}')

    @lambda_app.command()
    def invoke(
        function: Annotated[str, typer.Argument(help='Function alias from config')],
        payload: Annotated[str, typer.Argument(help='JSON payload')] = '{}',
    ) -> None:
        """Invoke a Lambda function and print the response."""
        from dectl.session import make_session

        fn_config = resolve_function(function)
        session = make_session(config)
        client = session.client('lambda')

        resp = client.invoke(
            FunctionName=fn_config.name,
            Payload=payload.encode(),
        )
        result = json.loads(resp['Payload'].read())
        console.print_json(json.dumps(result, indent=2))

    @lambda_app.command()
    def logs(
        function: Annotated[str, typer.Argument(help='Function alias from config')],
    ) -> None:
        """Tail CloudWatch logs for a Lambda function."""
        from dectl.session import make_session

        fn_config = resolve_function(function)
        session = make_session(config)
        logs_client = session.client('logs')
        tail_lambda_logs(logs_client, fn_config.name, follow=True)

    return lambda_app
