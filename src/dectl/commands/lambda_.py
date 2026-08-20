import json
import tempfile
import zipfile
from pathlib import Path
from typing import Annotated

import typer

from dectl.config import DectlConfig
from dectl.config import LambdaConfig
from dectl.durable import EXECUTION_STATUSES
from dectl.durable import epoch_millis
from dectl.durable import execution_to_dict
from dectl.durable import fetch_history
from dectl.durable import invoke_qualifier
from dectl.durable import list_executions
from dectl.durable import listing_qualifier
from dectl.durable import render_execution_header
from dectl.durable import render_executions_table
from dectl.durable import resolve_execution
from dectl.durable import sweep_executions
from dectl.durable import tail_durable_history
from dectl.env import render_env_model
from dectl.logs import DURABLE_OPERATION_ID_KEYS
from dectl.logs import tail_lambda_logs
from dectl.output import console
from dectl.output import emit_json
from dectl.output import error
from dectl.output import info
from dectl.output import success
from dectl.payloads import read_payload

# Widen the CloudWatch window around an execution's recorded start and end. The records are
# written by the function while the timestamps come from the control plane, so the two are close
# but not ordered; a minute either side costs nothing and never clips the first or last line.
LOG_WINDOW_BUFFER_MS = 60_000


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


def report_invocation(response: dict, as_json: bool) -> None:
    """Print an invoke response, exiting non-zero when the function itself failed.

    A handled or unhandled exception in the function still returns HTTP 200 with the error in the
    payload; FunctionError is the only signal it failed. Surfacing it as a non-zero exit is what
    lets a script (or an LLM) tell a failed invocation from a successful one."""
    raw = response['Payload'].read()
    if not raw:
        return
    result = json.loads(raw)

    if response.get('FunctionError'):
        if as_json:
            emit_json(result)
        else:
            error(f'function returned an error ({response["FunctionError"]}):')
            console.print_json(json.dumps(result, indent=2))
        raise typer.Exit(1)

    if as_json:
        emit_json(result)
    else:
        console.print_json(json.dumps(result, indent=2))


def make_lambda_function_app(pipeline_name: str, alias: str, fn_config: LambdaConfig, config: DectlConfig) -> typer.Typer:
    """Build the per-function sub-app: `dectl PIPELINE lambda <alias> <verb>`.

    Verbs close over this function's config and resolve {env} at call time. A function flagged
    `durable` gets a different set of them — see add_durable_verbs — because its unit of work is
    the execution rather than the invocation."""
    kind = 'Durable function' if fn_config.durable else 'Lambda function'
    fn_app = typer.Typer(
        no_args_is_help=True,
        help=f'{kind} [bold]{alias}[/bold] → {fn_config.name}',
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

    if fn_config.durable:
        add_durable_verbs(fn_app, pipeline_name, alias, config, resolved)
    else:
        add_standard_verbs(fn_app, pipeline_name, alias, config, resolved)

    return fn_app


def add_standard_verbs(fn_app: typer.Typer, pipeline_name: str, alias: str, config: DectlConfig, resolved) -> None:
    """run and logs for an ordinary function, where one invocation is the whole unit of work."""

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

        report_invocation(client.invoke(FunctionName=fn.name, Payload=payload.encode()), as_json)

    @fn_app.command(epilog=f'Example:\n\ndectl {pipeline_name} lambda {alias} logs --follow')
    def logs(
        follow: Annotated[bool, typer.Option('--follow', '-f', help='Keep tailing new log events (Ctrl-C to stop).')] = False,
    ) -> None:
        """Show CloudWatch logs for a Lambda function's recent activity (add --follow to tail)."""
        from dectl.session import make_session

        fn = resolved()
        logs_client = make_session(config).client('logs')
        tail_lambda_logs(logs_client, fn.name, follow=follow, hide_keys=frozenset())


def add_durable_verbs(fn_app: typer.Typer, pipeline_name: str, alias: str, config: DectlConfig, resolved) -> None:
    """run, executions, history and logs for a durable function.

    These replace the ordinary run/logs rather than sitting beside them, because for a durable
    function neither of those questions has an invocation-shaped answer: one execution spans many
    invocations, so `executions` is what tells you whether the work succeeded, and `logs` is only
    legible once scoped to a single execution."""

    @fn_app.command(
        epilog=(
            'Examples:\n\n'
            f'dectl {pipeline_name} lambda {alias} run --payload-file event.json\n\n'
            f'dectl {pipeline_name} lambda {alias} run --async --name order-123 — start and return\n\n'
            f'dectl {pipeline_name} lambda {alias} run --async --follow — start and watch the steps'
        ),
    )
    def run(
        payload_file: Annotated[
            str | None,
            typer.Option('--payload-file', help='JSON event: a file path, or - for stdin. Defaults to {}.'),
        ] = None,
        run_async: Annotated[
            bool,
            typer.Option('--async', help='Queue the event and return immediately, lifting the 15-minute synchronous cap.'),
        ] = False,
        name: Annotated[
            str | None,
            typer.Option('--name', help='Execution name. Re-running with the same name resumes rather than starting again.'),
        ] = None,
        follow: Annotated[bool, typer.Option('--follow', '-f', help="Tail the execution's step history until it finishes.")] = False,
        as_json: Annotated[bool, typer.Option('--json', help='Emit the response as machine-readable JSON to stdout.')] = False,
    ) -> None:
        """Start a durable execution and print its ARN.

        The invocation is qualified with the live alias when one is configured, falling back to
        $LATEST: Lambda rejects an unqualified invoke of a durable function, because an execution
        is pinned to the version it starts on so that a replay hours or days later runs the same
        code. Synchronous invocation waits for the whole execution and is capped at 15 minutes —
        use --async for anything longer.
        """
        from dectl.session import make_session

        fn = resolved()
        payload = read_payload(payload_file)
        client = make_session(config).client('lambda')

        kwargs: dict = {'FunctionName': fn.name, 'Qualifier': invoke_qualifier(fn), 'Payload': payload.encode()}
        if run_async:
            kwargs['InvocationType'] = 'Event'
        if name:
            kwargs['DurableExecutionName'] = name

        resp = client.invoke(**kwargs)
        execution_arn = resp.get('DurableExecutionArn')
        # Suppressed under --json so the response payload stays the only thing on stdout, the
        # same reason warnings go to stderr elsewhere. The ARN is in `executions --json`.
        if not as_json:
            if execution_arn:
                info(f'durable execution: {execution_arn}')
            else:
                info(f'queued on {fn.name}:{invoke_qualifier(fn)} — see "{pipeline_name} lambda {alias} executions"')

        report_invocation(resp, as_json)

        if follow and execution_arn:
            tail_durable_history(client, execution_arn, follow=True)

    @fn_app.command(
        epilog=(
            'Examples:\n\n'
            f'dectl {pipeline_name} lambda {alias} executions\n\n'
            f'dectl {pipeline_name} lambda {alias} executions --status FAILED --limit 20\n\n'
            f'dectl {pipeline_name} lambda {alias} executions --all-versions — across recent deploys'
        ),
    )
    def executions(
        status: Annotated[
            str | None,
            typer.Option('--status', help=f'Only executions in this state: {", ".join(EXECUTION_STATUSES)}.'),
        ] = None,
        limit: Annotated[int, typer.Option('--limit', '-n', help='Number of executions to show.')] = 10,
        qualifier: Annotated[
            str | None,
            typer.Option('--qualifier', help='Version, $LATEST, or an alias to resolve. Defaults to the configured live alias.'),
        ] = None,
        all_versions: Annotated[
            bool,
            typer.Option('--all-versions', help='Merge executions across recent published versions, not just the live one.'),
        ] = False,
        as_json: Annotated[bool, typer.Option('--json', help='Emit machine-readable JSON to stdout.')] = False,
    ) -> None:
        """List this function's durable executions with their status and elapsed time.

        Executions are keyed by the function *version* they ran on — Lambda resolves an alias to a
        version when the execution starts, so an alias cannot be listed directly and is resolved
        here to whatever version it currently points at. That means `deploy --publish` moves the
        listing to the new version and leaves earlier runs under the old one; `--all-versions`
        merges recent versions to find them.
        """
        from dectl.session import make_session

        fn = resolved()
        client = make_session(config).client('lambda')

        if all_versions:
            found, scanned = sweep_executions(client, fn.name, limit=limit, status=status)
            scope = f'versions {", ".join(scanned)}'
        else:
            asked = qualifier or invoke_qualifier(fn)
            version = listing_qualifier(client, fn.name, asked)
            found = list_executions(client, fn.name, version, limit=limit, status=status)
            scope = asked if asked == version else f'{asked} → version {version}'

        if as_json:
            emit_json([execution_to_dict(execution) for execution in found])
            return
        if not found:
            info(f'no durable executions for {fn.name} ({scope})')
            if not all_versions:
                info('add --all-versions to look across earlier deploys')
            return
        render_executions_table(alias, scope, found)

    @fn_app.command(
        epilog=(
            'Examples:\n\n'
            f'dectl {pipeline_name} lambda {alias} history — the most recent execution\n\n'
            f'dectl {pipeline_name} lambda {alias} history order-123 --follow'
        ),
    )
    def history(
        execution: Annotated[
            str | None,
            typer.Argument(help='Execution name, a unique tail of one, or an ARN. Defaults to the most recent.'),
        ] = None,
        follow: Annotated[bool, typer.Option('--follow', '-f', help='Keep polling while the execution runs.')] = False,
        no_data: Annotated[
            bool,
            typer.Option('--no-data', help='Omit step results and callback payloads, for a workflow with large ones.'),
        ] = False,
        as_json: Annotated[bool, typer.Option('--json', help='Emit machine-readable JSON to stdout.')] = False,
    ) -> None:
        """Show one execution's steps, waits, retries and failures in order.

        This is the checkpoint log Lambda replays from, so it is the record of what the workflow
        actually did — which step failed, what it returned, how long a wait suspended for. The
        `logs` verb is the complement: what the function's own logger printed while doing it.
        """
        from dectl.session import make_session

        fn = resolved()
        client = make_session(config).client('lambda')
        live_version = listing_qualifier(client, fn.name, invoke_qualifier(fn))
        found = resolve_execution(client, fn.name, live_version, execution)

        if as_json:
            emit_json(fetch_history(client, found['DurableExecutionArn'], include_data=not no_data))
            return

        render_execution_header(found)
        # A finished execution has nothing further to poll for, so --follow on one would spin
        # forever rather than terminate on the event it is already showing.
        still_running = found.get('EndTimestamp') is None
        tail_durable_history(client, found['DurableExecutionArn'], follow=follow and still_running, include_data=not no_data)

    @fn_app.command(
        epilog=(
            'Examples:\n\n'
            f'dectl {pipeline_name} lambda {alias} logs — logger output for the most recent execution\n\n'
            f'dectl {pipeline_name} lambda {alias} logs order-123\n\n'
            f'dectl {pipeline_name} lambda {alias} logs --all — the whole log group, unscoped'
        ),
    )
    def logs(
        execution: Annotated[
            str | None,
            typer.Argument(help='Execution name, a unique tail of one, or an ARN. Defaults to the most recent.'),
        ] = None,
        follow: Annotated[bool, typer.Option('--follow', '-f', help='Keep tailing while the execution runs.')] = False,
        every: Annotated[
            bool,
            typer.Option('--all', help='Tail the raw log group instead, across every execution.'),
        ] = False,
        context: Annotated[
            bool,
            typer.Option('--context', help='Keep every field the record carried, including the ones folded away by default.'),
        ] = False,
    ) -> None:
        """Show the function's own logger output for one durable execution.

        The SDK logger stamps the execution ARN onto every record, which is what the console's
        "Logger output" tab filters on and what this filters on too — so a log group carrying
        dozens of interleaved executions reads back as just this one. Platform START/END/REPORT
        lines carry no ARN and are excluded with everything else; the checkpointed step
        transitions are in `history`, not here.

        Those same stamped fields are identical on every line of a scoped tail, so they are
        folded away: the operation shows as a tag beside the level, and the rest is behind
        --context. `--all` keeps the execution ARN, since across executions it varies.
        """
        from dectl.session import make_session

        fn = resolved()
        session = make_session(config)
        logs_client = session.client('logs')

        # tail_lambda_logs adds the execution ARN to this when its filter pattern says the tail is
        # scoped to one execution, so there is one set here rather than a scoped and unscoped pair.
        hide_keys = frozenset() if context else DURABLE_OPERATION_ID_KEYS

        if every:
            tail_lambda_logs(logs_client, fn.name, follow=follow, hide_keys=hide_keys)
            return

        lambda_client = session.client('lambda')
        live_version = listing_qualifier(lambda_client, fn.name, invoke_qualifier(fn))
        found = resolve_execution(lambda_client, fn.name, live_version, execution)
        render_execution_header(found)

        started = epoch_millis(found.get('StartTimestamp'))
        ended = epoch_millis(found.get('EndTimestamp'))
        tail_lambda_logs(
            logs_client,
            fn.name,
            follow=follow and ended is None,
            filter_pattern=f'"{found["DurableExecutionArn"]}"',
            start_time=started - LOG_WINDOW_BUFFER_MS if started else None,
            end_time=ended + LOG_WINDOW_BUFFER_MS if ended else None,
            hide_keys=hide_keys,
        )


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
