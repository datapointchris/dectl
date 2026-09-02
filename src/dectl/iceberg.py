"""Iceberg table state, read from the table's own metadata file.

An Iceberg table's committed history lives in a JSON metadata file in S3, and Glue holds only a
pointer to it: the `metadata_location` parameter on the catalog table. So every question about
what a table has done — which commit changed the row count, when the file count started climbing,
whether the table was rolled back — is answered by GetTable followed by GetObject.

The metadata file carries the snapshots with their per-commit summaries, the snapshot log
recording every change of the current pointer, the refs holding branches and tags, and the schema
history. Everything *below* it — the manifest list, and the manifests naming individual data
files — is Avro, and this module does not cross that boundary. File-level questions are answered
from the per-snapshot summary counters instead, which is why `files` reports the table's file
layout per commit rather than a row per data file.
"""

import datetime as dt
import gzip
import json
from dataclasses import dataclass
from typing import Any

import typer
from botocore.exceptions import ClientError
from rich.markup import escape
from rich.table import Table

from dectl.output import console
from dectl.output import error
from dectl.output import format_duration
from dectl.output import info
from dectl.values import s3_uri

# Glue table parameters that mark a catalog entry as Iceberg and point at its metadata file.
TABLE_TYPE_KEY = 'table_type'
METADATA_LOCATION_KEY = 'metadata_location'
ICEBERG_TABLE_TYPE = 'iceberg'

OPERATION_COLORS = {'append': 'green', 'replace': 'cyan', 'overwrite': 'yellow', 'delete': 'red'}

# Summary counters read in more than one place. Iceberg writes every summary value as a string,
# so these are always run through summary_int rather than used directly.
TOTAL_RECORDS = 'total-records'
TOTAL_DATA_FILES = 'total-data-files'
TOTAL_DELETE_FILES = 'total-delete-files'
TOTAL_FILES_SIZE = 'total-files-size'

# The counters a diff subtracts, in the order they are printed. Records first because it is the
# number an incident starts from; the delete counters last because they only move on a
# merge-on-read table and are blank everywhere else.
DIFF_COUNTERS = (
    ('records', TOTAL_RECORDS),
    ('data files', TOTAL_DATA_FILES),
    ('file bytes', TOTAL_FILES_SIZE),
    ('delete files', TOTAL_DELETE_FILES),
    ('position deletes', 'total-position-deletes'),
    ('equality deletes', 'total-equality-deletes'),
)

BYTE_COUNTERS = frozenset({TOTAL_FILES_SIZE})

# How a gzipped metadata file is spelled. Iceberg puts the codec extension *before*
# `.metadata.json` — Java's TableMetadataParser builds the name as codec extension plus
# `.metadata.json`, and pyiceberg decompresses on exactly `.gz.metadata.json` and on nothing
# else. The trailing form is the legacy spelling, which Java still reads and no current writer
# produces. Both are accepted here because the catalog decides which one it points at.
GZIP_SUFFIXES = ('.gz.metadata.json', '.metadata.json.gz')


@dataclass(frozen=True)
class TableMetadata:
    """One Iceberg table's metadata document, with the S3 object it was read from.

    The location travels with the document because it is what makes a read auditable. The
    document is a point-in-time file, so a table committed to between two dectl invocations
    answers the same question from a different file under the same alias."""

    document: dict
    location: str

    @property
    def format_version(self) -> int | None:
        return self.document.get('format-version')

    @property
    def snapshots(self) -> list[dict]:
        return self.document.get('snapshots') or []

    @property
    def current_snapshot_id(self) -> int | None:
        # A writer with nothing committed may spell "no current snapshot" as -1 rather than
        # omitting the key, and -1 is not a snapshot id anyone can look up.
        found = self.document.get('current-snapshot-id')
        return None if found is None or found < 0 else found

    @property
    def snapshot_log(self) -> list[dict]:
        return self.document.get('snapshot-log') or []

    @property
    def refs(self) -> dict[str, dict]:
        return self.document.get('refs') or {}

    def by_id(self, snapshot_id: int | None) -> dict | None:
        if snapshot_id is None:
            return None
        return next((s for s in self.snapshots if s.get('snapshot-id') == snapshot_id), None)


def parse_s3_uri(uri: str) -> tuple[str, str]:
    """Split an s3://bucket/key URI into its bucket and key."""
    bucket, _, key = uri.removeprefix('s3://').partition('/')
    return bucket, key


def locate_metadata(glue_client, database: str, table: str) -> str:
    """The S3 URI of the table's current metadata file, from the Glue catalog entry.

    Glue is the catalog rather than the store, so this is the only thing it holds about an
    Iceberg table's state. A catalog entry missing either parameter is not an Iceberg table, and
    saying so is worth more than a KeyError on a parameter name nobody has heard of."""
    try:
        glue_table = glue_client.get_table(DatabaseName=database, Name=table)['Table']
    except ClientError as exc:
        if exc.response.get('Error', {}).get('Code') != 'EntityNotFoundException':
            raise
        error(f'no table {database}.{table} in the Glue Data Catalog')
        raise typer.Exit(1) from exc

    parameters = glue_table.get('Parameters') or {}
    table_type = parameters.get(TABLE_TYPE_KEY, '')
    if table_type.lower() != ICEBERG_TABLE_TYPE:
        found = f'its {TABLE_TYPE_KEY} is {escape(table_type)}' if table_type else f'it carries no {TABLE_TYPE_KEY} parameter'
        error(f'{database}.{table} is not an Iceberg table — {found}')
        error('  dectl reads Iceberg state from the table metadata file, which a plain Glue table does not have')
        raise typer.Exit(1)

    location = parameters.get(METADATA_LOCATION_KEY)
    if not location:
        error(f'{database}.{table} is registered as Iceberg but has no {METADATA_LOCATION_KEY} parameter')
        error('  nothing in the catalog says where its metadata file is, so its state cannot be read')
        raise typer.Exit(1)
    return location


def read_metadata_object(s3_client, location: str, database: str, table: str) -> TableMetadata:
    """Fetch and parse the metadata file the catalog points at.

    A gzipped metadata file is a table property rather than an exception, so the codec is taken
    from the key the catalog gave us."""
    if not location.startswith('s3://'):
        error(f'{database}.{table} points its {METADATA_LOCATION_KEY} at {escape(location)}, which is not an S3 URI')
        raise typer.Exit(1)

    bucket, key = parse_s3_uri(location)
    try:
        body = s3_client.get_object(Bucket=bucket, Key=key)['Body'].read()
    except ClientError as exc:
        code = exc.response.get('Error', {}).get('Code', 'unknown')
        error(f'cannot read the metadata file for {database}.{table} ({code})')
        error(f'  {s3_uri(bucket, key)}')
        error('  the catalog points here, so reading the table needs s3:GetObject on that key')
        raise typer.Exit(1) from exc

    if key.endswith(GZIP_SUFFIXES):
        body = gzip.decompress(body)

    try:
        document = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        error(f'the metadata file for {database}.{table} is not readable JSON')
        error(f'  {s3_uri(bucket, key)}')
        raise typer.Exit(1) from exc
    return TableMetadata(document=document, location=location)


def read_table_metadata(glue_client, s3_client, database: str, table: str) -> TableMetadata:
    """Locate and read one Iceberg table's metadata: GetTable, then GetObject."""
    return read_metadata_object(s3_client, locate_metadata(glue_client, database, table), database, table)


def summary_int(snapshot: dict, key: str) -> int | None:
    """Read one summary counter as an integer.

    Iceberg writes every summary value as a string, so a counter arrives as '4000' rather than
    4000 and subtracting two of them raises while adding two of them concatenates."""
    raw = (snapshot.get('summary') or {}).get(key)
    if raw is None:
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def operation(snapshot: dict) -> str:
    return (snapshot.get('summary') or {}).get('operation', '')


def colored_operation_name(name: str) -> str:
    if not name:
        return ''
    color = OPERATION_COLORS.get(name, 'white')
    return f'[{color}]{escape(name)}[/{color}]'


def colored_operation(snapshot: dict) -> str:
    return colored_operation_name(operation(snapshot))


def epoch_datetime(millis: Any) -> dt.datetime | None:
    if millis is None:
        return None
    return dt.datetime.fromtimestamp(millis / 1000, tz=dt.UTC)


def snapshot_time(snapshot: dict) -> dt.datetime | None:
    return epoch_datetime(snapshot.get('timestamp-ms'))


def snapshot_stamp(snapshot: dict) -> str:
    moment = snapshot_time(snapshot)
    return moment.isoformat() if moment else ''


def snapshot_timestamp_ms(snapshot: dict) -> int:
    """The commit time a listing orders on.

    Named for the field rather than for the use, because ordering by id would be a different
    answer: an Iceberg snapshot id is a random 64-bit value, not a counter."""
    return snapshot.get('timestamp-ms') or 0


def limited(rows: list, limit: int) -> list:
    """Take the first `limit` rows, where 0 means every one of them.

    One reading of `--limit` for every verb of this resource. A sentinel may never steal a value
    the flag can otherwise mean, which leaves 0 free to mean "all" on a limit. A sentinel one
    verb honours and its sibling does not is worse than no sentinel at all: both help rows read
    the same, the two answers differ, and the verb that slices to nothing then prints an
    empty-state sentence that is false about the table."""
    return rows if limit == 0 else rows[:limit]


def limited_tail(rows: list, limit: int) -> list:
    """The last `limit` rows, under the reading `limited` documents.

    A log is read oldest-first, so its limit takes the most recent entries and still renders
    them in order. The first N of a log is never the part anybody wants."""
    return rows if limit == 0 else rows[-limit:]


def format_bytes(value: int | None) -> str:
    """A byte count in the largest binary unit that keeps it under four digits."""
    if value is None:
        return ''
    size = float(value)
    for unit in ('B', 'KiB', 'MiB', 'GiB', 'TiB', 'PiB'):
        if size < 1024:
            return f'{int(size)} B' if unit == 'B' else f'{size:.1f} {unit}'
        size /= 1024
    return f'{size:.1f} EiB'


def format_duration_ms(millis: Any) -> str:
    """A retention window in whole days, hours or minutes — whichever it divides into cleanly."""
    if millis is None:
        return ''
    seconds = int(millis) // 1000
    if seconds >= 86400:
        return f'{seconds // 86400}d'
    if seconds >= 3600:
        return f'{seconds // 3600}h'
    return f'{seconds // 60}m' if seconds >= 60 else f'{seconds}s'


def format_count(value: int | None) -> str:
    return '' if value is None else f'{value:,}'


def format_delta(value: int | None) -> str:
    """A signed change. Deliberately uncoloured: a falling record count is an incident on one
    table and the expected result of a delete on the next, so the sign is the whole signal."""
    return '' if value is None else f'{value:+,}'


def format_churn(added: int | None, deleted: int | None) -> str:
    """The records a commit added and removed, as one cell.

    A zero side is left out rather than printed as `+0`: the operation column already says what
    kind of commit it was, so a column of zeroes only crowds the numbers that moved."""
    parts = []
    if added:
        parts.append(f'{added:+,}')
    if deleted:
        parts.append(f'{-deleted:+,}')
    return ' '.join(parts)


def average_file_bytes(snapshot: dict) -> int | None:
    """Total file bytes over data files — the number that says whether a table needs compacting."""
    total = summary_int(snapshot, TOTAL_FILES_SIZE)
    files = summary_int(snapshot, TOTAL_DATA_FILES)
    if total is None or not files:
        return None
    return total // files


def ancestry(metadata: TableMetadata, start_id: int | None) -> list[dict]:
    """The snapshots from `start_id` back to the table's first, newest first, by parent pointer.

    Lineage is not the same as the snapshot log. A rollback appends a log entry pointing at an
    older snapshot without changing anybody's parent, so the rolled-back commits leave the
    lineage while staying in the log — which is what makes a rollback visible when the two are
    shown side by side. The seen-set is a cycle guard: a parent chain that loops would otherwise
    walk forever on a metadata file nobody can fix from here."""
    by_id = {s.get('snapshot-id'): s for s in metadata.snapshots}
    chain: list[dict] = []
    seen: set = set()
    current = start_id
    while current is not None and current in by_id and current not in seen:
        seen.add(current)
        snapshot = by_id[current]
        chain.append(snapshot)
        current = snapshot.get('parent-snapshot-id')
    return chain


def current_snapshot(metadata: TableMetadata) -> dict:
    found = metadata.by_id(metadata.current_snapshot_id)
    if found is None:
        error('this table has no current snapshot — nothing has been committed to it')
        raise typer.Exit(1)
    return found


def snapshot_by_ref(metadata: TableMetadata, name: str) -> dict:
    ref = metadata.refs.get(name)
    if ref is None:
        error(f'no branch or tag named {escape(name)} on this table')
        if metadata.refs:
            error(f'  refs: {", ".join(escape(known) for known in sorted(metadata.refs))}')
        error('  a snapshot id, or a unique tail of one, resolves here too')
        raise typer.Exit(1)
    found = metadata.by_id(ref.get('snapshot-id'))
    if found is None:
        error(f'{escape(name)} points at snapshot {ref.get("snapshot-id")}, which is not in this metadata file')
        error('  the snapshot it names has been expired, so nothing about it can be read')
        raise typer.Exit(1)
    return found


def snapshot_by_id_or_suffix(metadata: TableMetadata, digits: str) -> dict:
    """The snapshot with this id, or the one whose id ends in it.

    Snapshot ids are 19-digit longs, so the tail is the handle you can retype. Several matches is
    a usage error listing them, never a guess at which one was meant."""
    exact = metadata.by_id(int(digits))
    if exact is not None:
        return exact

    candidates = [s for s in metadata.snapshots if str(s.get('snapshot-id', '')).endswith(digits)]
    if not candidates:
        error(f'no snapshot {digits}, or ending in it, in this table')
        error(f'  the metadata file holds {len(metadata.snapshots)} snapshots; an expired one is not among them')
        raise typer.Exit(1)
    if len(candidates) > 1:
        error(f'{digits} matches {len(candidates)} snapshots; use more of the id:')
        for found in sorted(candidates, key=snapshot_timestamp_ms, reverse=True):
            error(f'  {found.get("snapshot-id")}  {snapshot_stamp(found)}')
        raise typer.Exit(2)
    return candidates[0]


def resolve_snapshot(metadata: TableMetadata, reference: str | None) -> dict:
    """Resolve a snapshot argument: nothing, a ref name, a full snapshot id, or a unique tail.

    All-decimal is read as a snapshot id and anything else as a branch or tag name, so the two
    namespaces never contend. Omitting it takes the current snapshot, matching `glue logs` and
    `sfn logs`.

    `isdecimal`, not `isdigit`: the latter is true for characters `int()` refuses, superscripts
    among them, so it routes a value into the id path that then raises on conversion. Anything
    it turns away is a ref lookup, which reports what it could not find."""
    if reference is None:
        return current_snapshot(metadata)
    if reference.isdecimal():
        return snapshot_by_id_or_suffix(metadata, reference)
    return snapshot_by_ref(metadata, reference)


def refs_pointing_at(metadata: TableMetadata, snapshot: dict) -> list[str]:
    snapshot_id = snapshot.get('snapshot-id')
    return sorted(name for name, ref in metadata.refs.items() if ref.get('snapshot-id') == snapshot_id)


def snapshot_to_dict(metadata: TableMetadata, snapshot: dict) -> dict:
    """The stable --json shape for one snapshot.

    The raw summary is carried alongside the named counters because writers add their own keys to
    it, and a fact the table cannot show is still reachable this way."""
    return {
        'snapshot_id': snapshot.get('snapshot-id'),
        'parent_snapshot_id': snapshot.get('parent-snapshot-id'),
        'sequence_number': snapshot.get('sequence-number'),
        'timestamp': snapshot_time(snapshot),
        'operation': operation(snapshot),
        'schema_id': snapshot.get('schema-id'),
        'added_records': summary_int(snapshot, 'added-records'),
        'deleted_records': summary_int(snapshot, 'deleted-records'),
        'total_records': summary_int(snapshot, TOTAL_RECORDS),
        'added_data_files': summary_int(snapshot, 'added-data-files'),
        'deleted_data_files': summary_int(snapshot, 'deleted-data-files'),
        'total_data_files': summary_int(snapshot, TOTAL_DATA_FILES),
        'total_delete_files': summary_int(snapshot, TOTAL_DELETE_FILES),
        'total_files_size': summary_int(snapshot, TOTAL_FILES_SIZE),
        'average_file_size': average_file_bytes(snapshot),
        'refs': refs_pointing_at(metadata, snapshot),
        'summary': (snapshot.get('summary') or {}),
    }


def render_snapshots_table(alias: str, metadata: TableMetadata, snapshots: list[dict]) -> None:
    """Snapshots newest first, with the refs that point at each one.

    The id shows in full and folds rather than truncating: every other verb accepts its tail as a
    handle, and an ellipsis would hide exactly the end you would retype. The refs column is what
    makes an unfamiliar list readable — it says which of these the table is actually on."""
    table = Table(title=f'{alias} snapshots')
    table.add_column('snapshot', overflow='fold')
    table.add_column('when')
    table.add_column('operation')
    table.add_column('records ±')
    table.add_column('total records', justify='right')
    table.add_column('data files', justify='right')
    table.add_column('refs')
    for snapshot in snapshots:
        churn = format_churn(summary_int(snapshot, 'added-records'), summary_int(snapshot, 'deleted-records'))
        table.add_row(
            str(snapshot.get('snapshot-id', '')),
            snapshot_stamp(snapshot),
            colored_operation(snapshot),
            churn,
            format_count(summary_int(snapshot, TOTAL_RECORDS)),
            format_count(summary_int(snapshot, TOTAL_DATA_FILES)),
            ', '.join(escape(name) for name in refs_pointing_at(metadata, snapshot)),
        )
    console.print(table)


def history_rows(metadata: TableMetadata, limit: int) -> list[dict]:
    """The snapshot log, oldest first, with each entry marked as a current ancestor or not.

    The log records every change of the table's current pointer, so a rollback appears in it as a
    fresh entry naming an older snapshot. Marking ancestry against the current lineage is what
    separates the two: an entry that is no longer an ancestor is a commit the table has left
    behind."""
    ancestors = {s.get('snapshot-id') for s in ancestry(metadata, metadata.current_snapshot_id)}
    entries = limited_tail(metadata.snapshot_log, limit)
    rows = []
    for entry in entries:
        snapshot_id = entry.get('snapshot-id')
        snapshot = metadata.by_id(snapshot_id) or {}
        rows.append(
            {
                'timestamp': epoch_datetime(entry.get('timestamp-ms')),
                'snapshot_id': snapshot_id,
                'operation': operation(snapshot),
                'is_current_ancestor': snapshot_id in ancestors,
            }
        )
    return rows


def render_history_table(alias: str, rows: list[dict]) -> None:
    table = Table(title=f'{alias} history')
    table.add_column('when')
    table.add_column('snapshot', overflow='fold')
    table.add_column('operation')
    table.add_column('ancestor')
    for row in rows:
        moment = row['timestamp']
        table.add_row(
            moment.isoformat() if moment else '',
            str(row['snapshot_id'] or ''),
            colored_operation_name(row['operation']),
            'yes' if row['is_current_ancestor'] else '[yellow]no[/yellow]',
        )
    console.print(table)


def retention_summary(ref: dict) -> str:
    """A ref's own retention overrides, blank where the table's defaults apply."""
    parts = []
    if ref.get('min-snapshots-to-keep') is not None:
        parts.append(f'keep >= {ref["min-snapshots-to-keep"]}')
    if ref.get('max-snapshot-age-ms') is not None:
        parts.append(f'snapshots <= {format_duration_ms(ref["max-snapshot-age-ms"])}')
    if ref.get('max-ref-age-ms') is not None:
        parts.append(f'ref <= {format_duration_ms(ref["max-ref-age-ms"])}')
    return ', '.join(parts)


def branch_rows(metadata: TableMetadata) -> list[dict]:
    """Every branch and tag, branches first, each with the snapshot it points at."""
    rows = []
    for name, ref in metadata.refs.items():
        snapshot = metadata.by_id(ref.get('snapshot-id')) or {}
        rows.append(
            {
                'name': name,
                'type': (ref.get('type') or '').lower(),
                'snapshot_id': ref.get('snapshot-id'),
                'timestamp': snapshot_time(snapshot),
                'operation': operation(snapshot),
                'is_current': ref.get('snapshot-id') == metadata.current_snapshot_id,
                'retention': retention_summary(ref),
            }
        )
    return sorted(rows, key=lambda row: (row['type'] != 'branch', row['name']))


def render_branches_table(alias: str, rows: list[dict]) -> None:
    table = Table(title=f'{alias} branches and tags')
    table.add_column('ref')
    table.add_column('type')
    table.add_column('snapshot', overflow='fold')
    table.add_column('when')
    table.add_column('current')
    table.add_column('retention')
    for row in rows:
        moment = row['timestamp']
        table.add_row(
            escape(row['name']),
            escape(row['type']),
            str(row['snapshot_id'] or ''),
            moment.isoformat() if moment else '',
            'yes' if row['is_current'] else '',
            escape(row['retention']),
        )
    console.print(table)


def file_rows(metadata: TableMetadata, snapshot: dict, limit: int) -> list[dict]:
    """The table's file layout at each commit, walking back from `snapshot` through its lineage."""
    return [
        {
            'snapshot_id': entry.get('snapshot-id'),
            'timestamp': snapshot_time(entry),
            'operation': operation(entry),
            'data_files': summary_int(entry, TOTAL_DATA_FILES),
            'delete_files': summary_int(entry, TOTAL_DELETE_FILES),
            'position_deletes': summary_int(entry, 'total-position-deletes'),
            'equality_deletes': summary_int(entry, 'total-equality-deletes'),
            'records': summary_int(entry, TOTAL_RECORDS),
            'total_bytes': summary_int(entry, TOTAL_FILES_SIZE),
            'average_file_bytes': average_file_bytes(entry),
        }
        for entry in limited(ancestry(metadata, snapshot.get('snapshot-id')), limit)
    ]


def render_files_table(alias: str, rows: list[dict]) -> None:
    """File layout per commit, newest first, so a rising file count reads down the column.

    Average file size is the compaction signal: a table whose files average a few hundred KiB is
    paying a per-file open on every read of it.

    Delete files only exist on a merge-on-read table, so the column is shown only where one of
    these commits reports any. Its absence is itself the answer for a copy-on-write table, and a
    column of blanks would crowd the numbers that moved. `--json` carries the counters either
    way, along with the position and equality breakdown this summarises."""
    has_deletes = any(row['delete_files'] for row in rows)
    table = Table(title=f'{alias} file layout')
    table.add_column('snapshot', overflow='fold')
    table.add_column('when')
    table.add_column('operation')
    table.add_column('data files', justify='right')
    if has_deletes:
        table.add_column('delete files', justify='right')
    table.add_column('records', justify='right')
    table.add_column('size', justify='right')
    table.add_column('avg file', justify='right')
    for row in rows:
        moment = row['timestamp']
        cells = [
            str(row['snapshot_id'] or ''),
            moment.isoformat() if moment else '',
            colored_operation_name(row['operation']),
            format_count(row['data_files']),
        ]
        if has_deletes:
            cells.append(format_count(row['delete_files']))
        cells += [format_count(row['records']), format_bytes(row['total_bytes']), format_bytes(row['average_file_bytes'])]
        table.add_row(*cells)
    console.print(table)


@dataclass(frozen=True)
class SnapshotDiff:
    """Two snapshots and the commits that ran between them.

    `path` is the commits after `base` up to and including `target`, oldest first. It is empty
    and `linear` is false where target does not descend from base — two branches, or a comparison
    made backwards — and the counter deltas are then a straight subtraction between two points
    rather than an account of what happened."""

    base: dict
    target: dict
    path: list[dict]
    linear: bool


def diff_snapshots(metadata: TableMetadata, base: dict, target: dict) -> SnapshotDiff:
    """Compare two snapshots, with the commits between them where one descends from the other.

    The path is what turns a row-count change into an explanation: three appends and an overwrite
    between yesterday and now is a different incident from one overwrite."""
    base_id = base.get('snapshot-id')
    walked: list[dict] = []
    linear = False
    for snapshot in ancestry(metadata, target.get('snapshot-id')):
        if snapshot.get('snapshot-id') == base_id:
            linear = True
            break
        walked.append(snapshot)
    # Only a walk that reached the base is a path between the two. One that ran out is the
    # target's whole lineage, and reporting that as the commits between them would be a
    # plausible-looking answer to a question that has none.
    return SnapshotDiff(base=base, target=target, path=walked[::-1] if linear else [], linear=linear)


def diff_counters(diff: SnapshotDiff) -> list[dict]:
    """Each tracked counter before, after, and changed — dropping the ones neither side reports."""
    rows = []
    for label, key in DIFF_COUNTERS:
        before = summary_int(diff.base, key)
        after = summary_int(diff.target, key)
        if before is None and after is None:
            continue
        change = after - before if before is not None and after is not None else None
        rows.append({'metric': label, 'key': key, 'before': before, 'after': after, 'change': change})
    return rows


def diff_to_dict(diff: SnapshotDiff, metadata: TableMetadata) -> dict:
    return {
        'base': snapshot_to_dict(metadata, diff.base),
        'target': snapshot_to_dict(metadata, diff.target),
        'linear': diff.linear,
        'elapsed_seconds': elapsed_seconds(diff),
        'schema_changed': diff.base.get('schema-id') != diff.target.get('schema-id'),
        'commits': [snapshot_to_dict(metadata, snapshot) for snapshot in diff.path],
        'counters': diff_counters(diff),
    }


def elapsed_seconds(diff: SnapshotDiff) -> float | None:
    start = snapshot_time(diff.base)
    end = snapshot_time(diff.target)
    return abs((end - start).total_seconds()) if start and end else None


def render_diff(alias: str, diff: SnapshotDiff) -> None:
    """The two endpoints, what ran between them, and every counter that moved."""
    base_id = diff.base.get('snapshot-id')
    target_id = diff.target.get('snapshot-id')
    info(f'[bold]{alias}[/bold]  {base_id} → {target_id}')
    elapsed = format_duration(snapshot_time(diff.base), snapshot_time(diff.target))
    info(f'  {snapshot_stamp(diff.base)} → {snapshot_stamp(diff.target)}{f"   ({elapsed})" if elapsed else ""}')

    if not diff.linear:
        info('  [yellow]not on one line[/yellow]: the second snapshot does not descend from the first,')
        info('  so the numbers below are two points compared, not an account of what ran between them')
    elif diff.path:
        operations = ', '.join(colored_operation(snapshot) for snapshot in diff.path)
        info(f'  {len(diff.path)} commit{"s" if len(diff.path) > 1 else ""} between them: {operations}')
    else:
        info('  the same snapshot on both sides — nothing ran between them')

    if diff.base.get('schema-id') != diff.target.get('schema-id'):
        info(f'  [yellow]schema changed[/yellow]: {diff.base.get("schema-id")} → {diff.target.get("schema-id")}')

    counters = diff_counters(diff)
    if not counters:
        info('  neither snapshot records any summary counters')
        return

    # Titled with the alias alone: the two lines above already name both snapshots, and 19-digit
    # ids in a title wrap it across the narrow table this is.
    table = Table(title=f'{alias} counters')
    table.add_column('metric')
    table.add_column('before', justify='right')
    table.add_column('after', justify='right')
    table.add_column('change', justify='right')
    for row in counters:
        is_bytes = row['key'] in BYTE_COUNTERS
        before = format_bytes(row['before']) if is_bytes else format_count(row['before'])
        after = format_bytes(row['after']) if is_bytes else format_count(row['after'])
        change = format_delta(row['change'])
        if is_bytes and row['change'] is not None:
            change = f'{"+" if row["change"] >= 0 else "-"}{format_bytes(abs(row["change"]))}'
        table.add_row(row['metric'], before, after, change)
    console.print(table)


def diff_endpoints(metadata: TableMetadata, base: str | None, target: str | None) -> tuple[dict, dict]:
    """Resolve the two ends of a diff, defaulting to "what did the last commit do".

    With nothing given the comparison is the current snapshot against its parent, which is the
    question asked most often and the only one that needs no arguments at all. One argument
    compares that snapshot forward to the current one."""
    if base is None and target is None:
        head = current_snapshot(metadata)
        parent = metadata.by_id(head.get('parent-snapshot-id'))
        if parent is None:
            error(f'snapshot {head.get("snapshot-id")} has no parent in this metadata file — it is the first commit')
            error('  name two snapshots to compare, or a branch or tag')
            raise typer.Exit(1)
        return parent, head
    if target is None:
        return resolve_snapshot(metadata, base), current_snapshot(metadata)
    return resolve_snapshot(metadata, base), resolve_snapshot(metadata, target)
