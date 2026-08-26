from typing import Annotated

import typer
from typer.models import OptionInfo

from dectl.config import DectlConfig
from dectl.config import IcebergTableConfig
from dectl.env import render_env_model
from dectl.iceberg import TableMetadata
from dectl.iceberg import branch_rows
from dectl.iceberg import diff_endpoints
from dectl.iceberg import diff_snapshots
from dectl.iceberg import diff_to_dict
from dectl.iceberg import file_rows
from dectl.iceberg import history_rows
from dectl.iceberg import limited
from dectl.iceberg import read_table_metadata
from dectl.iceberg import render_branches_table
from dectl.iceberg import render_diff
from dectl.iceberg import render_files_table
from dectl.iceberg import render_history_table
from dectl.iceberg import render_snapshots_table
from dectl.iceberg import resolve_snapshot
from dectl.iceberg import snapshot_timestamp_ms
from dectl.iceberg import snapshot_to_dict
from dectl.output import emit_json
from dectl.output import info
from dectl.session import make_session

# Rows a read shows by default, matching `glue runs` and `lambda executions`. Every verb here
# reads one metadata file however far back it looks, so the limit trims the display and never
# the fetch.
DEFAULT_LIMIT = 10


def limit_option(what: str) -> OptionInfo:
    """The `--limit` every verb of this resource takes.

    Built here rather than written per verb so no two of them can disagree about what the flag
    means — both help rows would still read the same, and the divergence would only show in the
    answers. `min=0` makes a negative a usage error rather than a silent off-by-one: -1 into a
    slice drops the last row and returns a plausible count nobody asked for."""
    return typer.Option('--limit', '-n', min=0, help=f'{what} 0 for all of them.')


def make_iceberg_table_app(pipeline_name: str, alias: str, table_config: IcebergTableConfig, config: DectlConfig) -> typer.Typer:
    """Build the per-table sub-app: `dectl PIPELINE iceberg <alias> <verb>`.

    Verbs close over this table's config and resolve {env} at call time. Each one reads the
    table's metadata file fresh, so two verbs run back to back can legitimately see different
    state — a table committed to between them has a new metadata file."""
    table_app = typer.Typer(
        no_args_is_help=True,
        help=f'Iceberg table [bold]{alias}[/bold] → {table_config.database}.{table_config.table}',
    )

    def resolved() -> IcebergTableConfig:
        return render_env_model(table_config)

    def load() -> TableMetadata:
        table = resolved()
        session = make_session(config)
        return read_table_metadata(session.client('glue'), session.client('s3'), table.database, table.table)

    @table_app.command(
        epilog=(
            'Examples:\n\n'
            f'dectl {pipeline_name} iceberg {alias} snapshots\n\n'
            f'dectl {pipeline_name} iceberg {alias} snapshots --limit 50 --json'
        ),
    )
    def snapshots(
        limit: Annotated[int, limit_option('Snapshots to show, newest first.')] = DEFAULT_LIMIT,
        as_json: Annotated[bool, typer.Option('--json', help='Emit machine-readable JSON to stdout.')] = False,
    ) -> None:
        """List the table's commits: what each one did, and how the row count moved.

        Every snapshot in the current metadata file is listed, including ones on other branches,
        with the refs column naming whichever branch or tag points at each. A snapshot that has
        been expired is gone from the file and cannot appear here.
        """
        metadata = load()
        found = limited(sorted(metadata.snapshots, key=snapshot_timestamp_ms, reverse=True), limit)

        if as_json:
            emit_json([snapshot_to_dict(metadata, snapshot) for snapshot in found])
            return
        if not found:
            info('no snapshots — nothing has been committed to this table')
            return
        render_snapshots_table(alias, metadata, found)

    @table_app.command(
        epilog=(f'Examples:\n\ndectl {pipeline_name} iceberg {alias} history\n\ndectl {pipeline_name} iceberg {alias} history --limit 50'),
    )
    def history(
        limit: Annotated[int, limit_option('Log entries to show, the most recent first.')] = DEFAULT_LIMIT,
        as_json: Annotated[bool, typer.Option('--json', help='Emit machine-readable JSON to stdout.')] = False,
    ) -> None:
        """Show every change of the table's current snapshot, oldest last, and which survive.

        This is the snapshot log rather than the lineage, and the difference is how a rollback
        shows up: rolling back appends a fresh entry naming an older snapshot, leaving the
        commits it skipped in the log but no longer ancestors of anything. The `ancestor` column
        is what marks them — a `no` is a commit the table has walked away from.
        """
        metadata = load()
        rows = history_rows(metadata, limit)

        if as_json:
            emit_json(rows)
            return
        if not rows:
            info('no history — nothing has been committed to this table')
            return
        render_history_table(alias, rows)

    @table_app.command(
        epilog=(
            'Examples:\n\n'
            f'dectl {pipeline_name} iceberg {alias} files — how the layout has moved over recent commits\n\n'
            f'dectl {pipeline_name} iceberg {alias} files main --limit 1 — the layout right now'
        ),
    )
    def files(
        snapshot: Annotated[
            str | None,
            typer.Argument(help='Snapshot id, a unique tail of one, or a branch or tag. Defaults to the current snapshot.'),
        ] = None,
        limit: Annotated[int, limit_option('Commits to walk back through.')] = DEFAULT_LIMIT,
        as_json: Annotated[bool, typer.Option('--json', help='Emit machine-readable JSON to stdout.')] = False,
    ) -> None:
        """Show the table's file layout at each commit: file counts, total size, average size.

        Average file size is the compaction signal — a table averaging a few hundred KiB a file
        pays a per-file open on every read of it — and reading down the column shows whether it
        is getting worse.

        These come from each snapshot's own summary counters, so this is the shape of the table's
        file layout rather than a listing of its data files. There is no row-per-file view: the
        manifests naming individual files are Avro, which dectl does not read.
        """
        metadata = load()
        rows = file_rows(metadata, resolve_snapshot(metadata, snapshot), limit)

        if as_json:
            emit_json(rows)
            return
        if not rows:
            info('no committed snapshots, so the table has no files')
            return
        render_files_table(alias, rows)

    @table_app.command(
        epilog=(
            'Examples:\n\n'
            f'dectl {pipeline_name} iceberg {alias} diff — what the last commit changed\n\n'
            f'dectl {pipeline_name} iceberg {alias} diff 4471 — from that snapshot to now\n\n'
            f'dectl {pipeline_name} iceberg {alias} diff 4471 9108 — between two snapshots'
        ),
    )
    def diff(
        base: Annotated[
            str | None,
            typer.Argument(help='Snapshot to compare from — an id, a unique tail of one, or a branch or tag.'),
        ] = None,
        target: Annotated[
            str | None,
            typer.Argument(help='Snapshot to compare to. Defaults to the current snapshot.'),
        ] = None,
        as_json: Annotated[bool, typer.Option('--json', help='Emit machine-readable JSON to stdout.')] = False,
    ) -> None:
        """Compare two snapshots: what ran between them, and every counter that moved.

        With no arguments this is the current snapshot against its parent — what the last commit
        did. With one, it runs from that snapshot forward to the current one, which is the shape
        of "the number went wrong yesterday": name yesterday's snapshot and read off the commits
        and the record delta since.

        Where the two are not on one line — different branches, or the arguments the other way
        round — the commit list is omitted and the counters are two points compared.
        """
        metadata = load()
        from_snapshot, to_snapshot = diff_endpoints(metadata, base, target)
        comparison = diff_snapshots(metadata, from_snapshot, to_snapshot)

        if as_json:
            emit_json(diff_to_dict(comparison, metadata))
            return
        render_diff(alias, comparison)

    @table_app.command(
        epilog=f'Example:\n\ndectl {pipeline_name} iceberg {alias} branches --json',
    )
    def branches(
        limit: Annotated[int, limit_option('Refs to show, branches before tags.')] = DEFAULT_LIMIT,
        as_json: Annotated[bool, typer.Option('--json', help='Emit machine-readable JSON to stdout.')] = False,
    ) -> None:
        """List the table's branches and tags with the snapshot each points at.

        Every ref shown here is a handle the other verbs accept, so a tag is a name you can diff
        from. The retention column carries only a ref's own overrides; a blank one expires under
        the table's defaults.
        """
        metadata = load()
        rows = limited(branch_rows(metadata), limit)

        if as_json:
            emit_json(rows)
            return
        if not rows:
            info('no branches or tags on this table')
            return
        render_branches_table(alias, rows)

    return table_app


def make_iceberg_app(pipeline_name: str, pipeline, config: DectlConfig) -> typer.Typer:
    iceberg_tables = pipeline.iceberg_tables
    alias_list = ', '.join(iceberg_tables.keys()) or '(none configured)'

    iceberg_app = typer.Typer(
        no_args_is_help=True,
        help=f'Iceberg tables in [bold]{pipeline_name}[/bold] — pick a table, then a verb.\n\nTables: {alias_list}',
    )
    for alias, table_config in iceberg_tables.items():
        iceberg_app.add_typer(
            make_iceberg_table_app(pipeline_name, alias, table_config, config),
            name=alias,
            rich_help_panel='Tables',
        )
    return iceberg_app
