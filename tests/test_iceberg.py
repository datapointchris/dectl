import gzip
import io
import json

import pytest
import typer
from botocore.exceptions import ClientError
from typer.testing import CliRunner

from dectl.commands.iceberg import make_iceberg_app
from dectl.config import DectlConfig
from dectl.env import active_environment
from dectl.iceberg import TableMetadata
from dectl.iceberg import ancestry
from dectl.iceberg import average_file_bytes
from dectl.iceberg import branch_rows
from dectl.iceberg import diff_endpoints
from dectl.iceberg import diff_snapshots
from dectl.iceberg import file_rows
from dectl.iceberg import format_bytes
from dectl.iceberg import format_churn
from dectl.iceberg import history_rows
from dectl.iceberg import parse_s3_uri
from dectl.iceberg import read_table_metadata
from dectl.iceberg import render_diff
from dectl.iceberg import render_files_table
from dectl.iceberg import render_snapshots_table
from dectl.iceberg import resolve_snapshot
from dectl.iceberg import snapshot_to_dict
from dectl.iceberg import summary_int
from dectl.output import console

runner = CliRunner()

# Realistic 19-digit Iceberg snapshot ids, whose tails are the handles the verbs accept.
SNAP_ONE = 3821550127947081234
SNAP_TWO = 5140398211772294471
SNAP_THREE = 7719023845110039108
SNAP_FOUR = 2264910337788105567

BASE_MS = 1767225600000
HOUR_MS = 3600000

METADATA_KEY = 'warehouse/events/metadata/00004-abc.metadata.json'
METADATA_URI = f's3://example-lake/{METADATA_KEY}'


@pytest.fixture(autouse=True)
def wide_console(monkeypatch):
    """Render tables at a width that fits every column.

    rich squeezes columns to the terminal, and at the default 80 an assertion on a value in a
    late column fails for a reason that has nothing to do with the code under test."""
    monkeypatch.setenv('COLUMNS', '220')


def snapshot(
    snapshot_id: int,
    at_ms: int,
    *,
    parent: int | None = None,
    sequence: int = 1,
    operation: str = 'append',
    schema_id: int = 0,
    **summary: int,
) -> dict:
    """One snapshot as a metadata file records it.

    Every summary value is written as a string, which is what the Iceberg spec says a writer
    produces and what a real metadata file holds. A fixture using integers here would let a
    reader that never coerces look correct: the arithmetic would work against these dicts and
    fail on the first real table, where subtracting two counters raises and adding two of them
    concatenates."""
    body = {
        'snapshot-id': snapshot_id,
        'sequence-number': sequence,
        'timestamp-ms': at_ms,
        'schema-id': schema_id,
        'manifest-list': f's3://example-lake/warehouse/events/metadata/snap-{snapshot_id}.avro',
        'summary': {'operation': operation, **{key.replace('_', '-'): str(value) for key, value in summary.items()}},
    }
    if parent is not None:
        body['parent-snapshot-id'] = parent
    return body


def metadata_document(
    snapshots: list[dict],
    *,
    current: int | None,
    log: list[tuple[int, int]] | None = None,
    refs: dict | None = None,
) -> dict:
    """A v2 table metadata document holding the given snapshots."""
    return {
        'format-version': 2,
        'table-uuid': '9a1f0f4e-4e0e-4f1a-9f6a-0b6a3c2d1e00',
        'location': 's3://example-lake/warehouse/events',
        'last-updated-ms': snapshots[-1]['timestamp-ms'] if snapshots else BASE_MS,
        'current-schema-id': 1,
        'schemas': [
            {'type': 'struct', 'schema-id': 0, 'fields': [{'id': 1, 'name': 'id', 'required': True, 'type': 'long'}]},
            {
                'type': 'struct',
                'schema-id': 1,
                'fields': [
                    {'id': 1, 'name': 'id', 'required': True, 'type': 'long'},
                    {'id': 2, 'name': 'amount', 'required': False, 'type': 'decimal(12, 2)'},
                ],
            },
        ],
        'default-spec-id': 0,
        'partition-specs': [{'spec-id': 0, 'fields': []}],
        'default-sort-order-id': 0,
        'sort-orders': [{'order-id': 0, 'fields': []}],
        'properties': {'write.format.default': 'parquet'},
        'current-snapshot-id': current,
        'snapshots': snapshots,
        'snapshot-log': [{'snapshot-id': sid, 'timestamp-ms': ts} for sid, ts in (log or [])],
        'metadata-log': [],
        'refs': refs or {},
    }


def four_commit_table() -> dict:
    """A table whose third commit is an overwrite that dropped rows — the incident shape.

    Records climb to 2400 across two appends, fall to 1800 on an overwrite, then recover to 2000.
    File count only ever climbs, which is what `files` is for."""
    snapshots = [
        snapshot(
            SNAP_ONE,
            BASE_MS,
            added_records=1200,
            total_records=1200,
            added_data_files=4,
            total_data_files=4,
            total_files_size=4194304,
        ),
        snapshot(
            SNAP_TWO,
            BASE_MS + HOUR_MS,
            parent=SNAP_ONE,
            sequence=2,
            added_records=1200,
            total_records=2400,
            added_data_files=5,
            total_data_files=9,
            total_files_size=9437184,
        ),
        snapshot(
            SNAP_THREE,
            BASE_MS + 2 * HOUR_MS,
            parent=SNAP_TWO,
            sequence=3,
            operation='overwrite',
            schema_id=1,
            added_records=600,
            deleted_records=1200,
            total_records=1800,
            added_data_files=2,
            deleted_data_files=0,
            total_data_files=11,
            total_files_size=11534336,
        ),
        snapshot(
            SNAP_FOUR,
            BASE_MS + 3 * HOUR_MS,
            parent=SNAP_THREE,
            sequence=4,
            schema_id=1,
            added_records=200,
            total_records=2000,
            added_data_files=3,
            total_data_files=14,
            total_files_size=12582912,
        ),
    ]
    return metadata_document(
        snapshots,
        current=SNAP_FOUR,
        log=[
            (SNAP_ONE, BASE_MS),
            (SNAP_TWO, BASE_MS + HOUR_MS),
            (SNAP_THREE, BASE_MS + 2 * HOUR_MS),
            (SNAP_FOUR, BASE_MS + 3 * HOUR_MS),
        ],
        refs={
            'main': {'snapshot-id': SNAP_FOUR, 'type': 'branch'},
            'nightly': {'snapshot-id': SNAP_TWO, 'type': 'branch', 'min-snapshots-to-keep': 3, 'max-snapshot-age-ms': 604800000},
            'month-end': {'snapshot-id': SNAP_ONE, 'type': 'tag', 'max-ref-age-ms': 2592000000},
        },
    )


def rolled_back_table() -> dict:
    """The same four commits, then a rollback to the second.

    The two later commits stay in the snapshot log and stop being ancestors of anything, which is
    the only signal in the file that a rollback happened."""
    document = four_commit_table()
    document['current-snapshot-id'] = SNAP_TWO
    document['snapshot-log'].append({'snapshot-id': SNAP_TWO, 'timestamp-ms': BASE_MS + 4 * HOUR_MS})
    document['refs']['main'] = {'snapshot-id': SNAP_TWO, 'type': 'branch'}
    return document


class FakeBody:
    """A GetObject body, which the real API hands back as a stream rather than as bytes."""

    def __init__(self, payload: bytes) -> None:
        self.stream = io.BytesIO(payload)

    def read(self) -> bytes:
        return self.stream.read()


class FakeGlue:
    """Glue stand-in for GetTable, refusing what the service refuses.

    An unknown table raises EntityNotFoundException rather than returning an empty result, and a
    table that is not Iceberg simply lacks the two parameters — which is the shape somebody hits
    on their first try, so a test has to be able to build it."""

    def __init__(self, tables: dict[str, dict] | None = None) -> None:
        self.tables = tables or {}
        self.calls: list[dict] = []

    def get_table(self, DatabaseName=None, Name=None) -> dict:
        if not DatabaseName or not Name:
            raise AssertionError('GetTable takes both DatabaseName and Name; boto3 rejects the call without them')
        self.calls.append({'DatabaseName': DatabaseName, 'Name': Name})
        key = f'{DatabaseName}.{Name}'
        if key not in self.tables:
            raise ClientError({'Error': {'Code': 'EntityNotFoundException', 'Message': f'Table {key} not found'}}, 'GetTable')
        return {'Table': self.tables[key]}


class FakeS3:
    """S3 stand-in for GetObject, enforcing the two things a permissive fake would let through.

    A body is a stream that has to be read, not bytes already in hand. And an object whose key
    ends in .gz holds gzipped bytes, because that is what `write.metadata.compression-codec=gzip`
    produces — a reader that skips the decompression passes against a fake handing back plain
    text and fails against the first real table configured that way."""

    def __init__(self, objects: dict[str, str] | None = None, denied: tuple[str, ...] = ()) -> None:
        self.objects = objects or {}
        self.denied = denied
        self.calls: list[dict] = []

    def get_object(self, Bucket=None, Key=None) -> dict:
        if not Bucket or not Key:
            raise AssertionError('GetObject takes both Bucket and Key; boto3 rejects the call without them')
        self.calls.append({'Bucket': Bucket, 'Key': Key})
        uri = f's3://{Bucket}/{Key}'
        if uri in self.denied:
            raise ClientError({'Error': {'Code': 'AccessDenied', 'Message': 'Access Denied'}}, 'GetObject')
        if uri not in self.objects:
            raise ClientError({'Error': {'Code': 'NoSuchKey', 'Message': 'The specified key does not exist.'}}, 'GetObject')
        payload = self.objects[uri].encode()
        return {'Body': FakeBody(gzip.compress(payload) if Key.endswith('.gz') else payload)}


class FakeSession:
    """A boto3 session over the two fakes, refusing a client this resource never asks for."""

    def __init__(self, glue: FakeGlue, s3: FakeS3) -> None:
        self.clients = {'glue': glue, 's3': s3}

    def client(self, name: str):
        if name not in self.clients:
            raise AssertionError(f'the iceberg resource asked for a {name} client, which it has no reason to need')
        return self.clients[name]


def iceberg_table_entry(metadata_uri: str = METADATA_URI, table_type: str = 'ICEBERG') -> dict:
    parameters = {'table_type': table_type}
    if metadata_uri:
        parameters['metadata_location'] = metadata_uri
    return {'Name': 'events', 'DatabaseName': 'lakehouse', 'Parameters': parameters}


def fakes_for(document: dict, key: str = METADATA_KEY) -> tuple[FakeGlue, FakeS3]:
    uri = f's3://example-lake/{key}'
    glue = FakeGlue({'lakehouse.events': iceberg_table_entry(uri)})
    s3 = FakeS3({uri: json.dumps(document)})
    return glue, s3


def load(document: dict) -> TableMetadata:
    glue, s3 = fakes_for(document)
    return read_table_metadata(glue, s3, 'lakehouse', 'events')


def make_config(tables: dict) -> DectlConfig:
    return DectlConfig.model_validate(
        {
            'defaults': {'account_id': '123456789012', 'region': 'us-east-2'},
            'pipelines': {'proj': {'iceberg_tables': tables}},
        }
    )


def make_app(document: dict, monkeypatch, tables: dict | None = None) -> tuple[typer.Typer, FakeGlue, FakeS3]:
    glue, s3 = fakes_for(document)
    config = make_config(tables or {'events': {'database': 'lakehouse', 'table': 'events'}})
    monkeypatch.setattr('dectl.commands.iceberg.make_session', lambda _config: FakeSession(glue, s3))
    return make_iceberg_app('proj', config.pipelines['proj'], config), glue, s3


# --- locating and reading the metadata file ---------------------------------------------------


def test_parse_s3_uri_splits_bucket_from_key():
    assert parse_s3_uri('s3://example-lake/warehouse/events/metadata/v1.json') == ('example-lake', 'warehouse/events/metadata/v1.json')


def test_reading_a_table_costs_one_get_table_and_one_get_object():
    glue, s3 = fakes_for(four_commit_table())

    read_table_metadata(glue, s3, 'lakehouse', 'events')

    assert glue.calls == [{'DatabaseName': 'lakehouse', 'Name': 'events'}]
    assert s3.calls == [{'Bucket': 'example-lake', 'Key': METADATA_KEY}]


def test_a_gzipped_metadata_file_reads():
    # write.metadata.compression-codec=gzip is a table property, not an exception, and the codec
    # is only knowable from the key the catalog handed back.
    key = 'warehouse/events/metadata/00004-abc.metadata.json.gz'
    glue, s3 = fakes_for(four_commit_table(), key=key)

    metadata = read_table_metadata(glue, s3, 'lakehouse', 'events')

    assert metadata.current_snapshot_id == SNAP_FOUR


def test_a_missing_table_names_the_table(capsys):
    glue, s3 = FakeGlue(), FakeS3()

    with pytest.raises(typer.Exit) as exc:
        read_table_metadata(glue, s3, 'lakehouse', 'events')

    assert exc.value.exit_code == 1
    assert 'lakehouse.events' in capsys.readouterr().out


def test_a_plain_glue_table_says_it_is_not_iceberg(capsys):
    # The first thing anybody does is point this at a table they already have. A KeyError on a
    # parameter name is the whole of what they would get.
    glue = FakeGlue({'lakehouse.events': {'Name': 'events', 'Parameters': {'classification': 'parquet'}}})

    with pytest.raises(typer.Exit) as exc:
        read_table_metadata(glue, FakeS3(), 'lakehouse', 'events')

    assert exc.value.exit_code == 1
    out = capsys.readouterr().out
    assert 'not an Iceberg table' in out
    assert 'no table_type parameter' in out


def test_a_table_of_another_format_names_the_format(capsys):
    glue = FakeGlue({'lakehouse.events': {'Name': 'events', 'Parameters': {'table_type': 'HUDI'}}})

    with pytest.raises(typer.Exit) as exc:
        read_table_metadata(glue, FakeS3(), 'lakehouse', 'events')

    assert exc.value.exit_code == 1
    assert 'HUDI' in capsys.readouterr().out


def test_an_iceberg_table_without_a_metadata_pointer_says_so(capsys):
    glue = FakeGlue({'lakehouse.events': iceberg_table_entry(metadata_uri='')})

    with pytest.raises(typer.Exit) as exc:
        read_table_metadata(glue, FakeS3(), 'lakehouse', 'events')

    assert exc.value.exit_code == 1
    assert 'metadata_location' in capsys.readouterr().out


def test_an_unreadable_metadata_file_names_the_bucket_and_key(capsys):
    # This is an IAM failure, and the key is the whole of what makes it fixable.
    glue = FakeGlue({'lakehouse.events': iceberg_table_entry()})
    s3 = FakeS3(denied=(METADATA_URI,))

    with pytest.raises(typer.Exit) as exc:
        read_table_metadata(glue, s3, 'lakehouse', 'events')

    assert exc.value.exit_code == 1
    out = capsys.readouterr().out
    assert 'AccessDenied' in out
    assert f's3://example-lake/{METADATA_KEY}' in out
    assert 's3:GetObject' in out


def test_a_metadata_pointer_that_is_not_an_s3_uri_refuses(capsys):
    glue = FakeGlue({'lakehouse.events': iceberg_table_entry(metadata_uri='/mnt/warehouse/v1.json')})

    with pytest.raises(typer.Exit) as exc:
        read_table_metadata(glue, FakeS3(), 'lakehouse', 'events')

    assert exc.value.exit_code == 1
    assert 'not an S3 URI' in capsys.readouterr().out


def test_a_metadata_file_that_is_not_json_refuses(capsys):
    glue = FakeGlue({'lakehouse.events': iceberg_table_entry()})
    s3 = FakeS3({METADATA_URI: 'PAR1\x00\x01not json at all'})

    with pytest.raises(typer.Exit) as exc:
        read_table_metadata(glue, s3, 'lakehouse', 'events')

    assert exc.value.exit_code == 1
    assert 'not readable JSON' in capsys.readouterr().out


# --- summary counters -------------------------------------------------------------------------


def test_summary_counters_come_back_as_integers():
    # Iceberg writes every summary value as a string. Without coercion `total-records` is '2000',
    # which compares, sorts and concatenates without ever raising.
    metadata = load(four_commit_table())
    head = metadata.by_id(SNAP_FOUR)

    assert summary_int(head, 'total-records') == 2000
    assert isinstance(summary_int(head, 'total-records'), int)


def test_a_counter_the_writer_did_not_record_is_none_rather_than_zero():
    # A merge-on-read counter missing from an append's summary means "not reported", and zero
    # would read as "measured and empty".
    metadata = load(four_commit_table())

    assert summary_int(metadata.by_id(SNAP_ONE), 'total-equality-deletes') is None


def test_average_file_size_is_bytes_over_data_files():
    metadata = load(four_commit_table())

    assert average_file_bytes(metadata.by_id(SNAP_FOUR)) == 12582912 // 14


def test_average_file_size_of_an_empty_commit_is_none():
    assert average_file_bytes(snapshot(1, BASE_MS, total_data_files=0, total_files_size=0)) is None


def test_format_bytes_uses_binary_units():
    assert format_bytes(512) == '512 B'
    assert format_bytes(4194304) == '4.0 MiB'
    assert format_bytes(None) == ''


def test_churn_omits_a_side_that_did_not_move():
    assert format_churn(1200, None) == '+1,200'
    assert format_churn(600, 1200) == '+600 -1,200'
    assert format_churn(None, None) == ''


# --- lineage, log and refs --------------------------------------------------------------------


def test_ancestry_walks_parent_pointers_newest_first():
    metadata = load(four_commit_table())

    assert [s['snapshot-id'] for s in ancestry(metadata, SNAP_FOUR)] == [SNAP_FOUR, SNAP_THREE, SNAP_TWO, SNAP_ONE]


def test_ancestry_survives_a_parent_chain_that_loops():
    # A cycle cannot happen in a file a writer produced, and walking one forever is a hang rather
    # than an error, so the guard is worth more than the case is likely.
    looped = metadata_document(
        [snapshot(1, BASE_MS, parent=2), snapshot(2, BASE_MS + 1, parent=1)],
        current=1,
    )

    assert len(ancestry(TableMetadata(document=looped, location=METADATA_URI), 1)) == 2


def test_history_marks_every_entry_as_an_ancestor_when_nothing_was_rolled_back():
    rows = history_rows(load(four_commit_table()), 10)

    assert [row['snapshot_id'] for row in rows] == [SNAP_ONE, SNAP_TWO, SNAP_THREE, SNAP_FOUR]
    assert all(row['is_current_ancestor'] for row in rows)


def test_history_shows_a_rollback_as_entries_that_stopped_being_ancestors():
    # This is the only trace a rollback leaves: the skipped commits stay in the log and drop out
    # of the lineage. Reading the log alone would show five entries and no sign anything changed.
    rows = history_rows(load(rolled_back_table()), 10)

    abandoned = {row['snapshot_id'] for row in rows if not row['is_current_ancestor']}
    assert abandoned == {SNAP_THREE, SNAP_FOUR}
    assert rows[-1]['snapshot_id'] == SNAP_TWO


def test_history_limit_takes_the_most_recent_entries_and_keeps_them_chronological():
    rows = history_rows(load(four_commit_table()), 2)

    assert [row['snapshot_id'] for row in rows] == [SNAP_THREE, SNAP_FOUR]


def test_branches_list_branches_before_tags_with_their_retention():
    rows = branch_rows(load(four_commit_table()))

    assert [row['name'] for row in rows] == ['main', 'nightly', 'month-end']
    assert rows[0]['is_current'] is True
    assert rows[1]['retention'] == 'keep >= 3, snapshots <= 7d'
    assert rows[2]['type'] == 'tag'
    assert rows[2]['retention'] == 'ref <= 30d'


# --- resolving a snapshot ---------------------------------------------------------------------


def test_no_argument_resolves_the_current_snapshot():
    assert resolve_snapshot(load(four_commit_table()), None)['snapshot-id'] == SNAP_FOUR


def test_a_full_snapshot_id_resolves():
    assert resolve_snapshot(load(four_commit_table()), str(SNAP_TWO))['snapshot-id'] == SNAP_TWO


def test_a_unique_tail_of_a_snapshot_id_resolves():
    # 19 digits is not a handle anybody retypes, and the tail is where the entropy is.
    assert resolve_snapshot(load(four_commit_table()), '4471')['snapshot-id'] == SNAP_TWO


def test_a_branch_or_tag_name_resolves():
    metadata = load(four_commit_table())

    assert resolve_snapshot(metadata, 'nightly')['snapshot-id'] == SNAP_TWO
    assert resolve_snapshot(metadata, 'month-end')['snapshot-id'] == SNAP_ONE


def test_an_ambiguous_tail_is_a_usage_error_listing_the_candidates(capsys):
    # Picking the newest would resolve silently to something the caller did not name.
    document = metadata_document(
        [snapshot(1111111111111119108, BASE_MS), snapshot(2222222222222229108, BASE_MS + HOUR_MS)],
        current=2222222222222229108,
    )

    with pytest.raises(typer.Exit) as exc:
        resolve_snapshot(TableMetadata(document=document, location=METADATA_URI), '9108')

    assert exc.value.exit_code == 2
    out = capsys.readouterr().out
    assert '1111111111111119108' in out
    assert '2222222222222229108' in out


def test_an_unknown_ref_lists_the_ones_that_exist(capsys):
    with pytest.raises(typer.Exit) as exc:
        resolve_snapshot(load(four_commit_table()), 'weekly')

    assert exc.value.exit_code == 1
    out = capsys.readouterr().out
    assert 'main' in out
    assert 'month-end' in out


def test_a_ref_pointing_at_an_expired_snapshot_says_so(capsys):
    document = metadata_document([snapshot(SNAP_ONE, BASE_MS)], current=SNAP_ONE, refs={'stale': {'snapshot-id': 999, 'type': 'tag'}})

    with pytest.raises(typer.Exit) as exc:
        resolve_snapshot(TableMetadata(document=document, location=METADATA_URI), 'stale')

    assert exc.value.exit_code == 1
    assert 'expired' in capsys.readouterr().out


def test_a_table_with_nothing_committed_says_so_rather_than_erroring_on_none(capsys):
    document = metadata_document([], current=None)

    with pytest.raises(typer.Exit) as exc:
        resolve_snapshot(TableMetadata(document=document, location=METADATA_URI), None)

    assert exc.value.exit_code == 1
    assert 'nothing has been committed' in capsys.readouterr().out


def test_a_current_snapshot_id_of_minus_one_reads_as_no_current_snapshot():
    # Some writers spell an empty table this way rather than omitting the key, and -1 is not an
    # id anything can look up.
    document = metadata_document([], current=-1)

    assert TableMetadata(document=document, location=METADATA_URI).current_snapshot_id is None


# --- diff -------------------------------------------------------------------------------------


def test_diff_with_no_arguments_compares_the_current_snapshot_to_its_parent():
    metadata = load(four_commit_table())

    base, target = diff_endpoints(metadata, None, None)

    assert (base['snapshot-id'], target['snapshot-id']) == (SNAP_THREE, SNAP_FOUR)


def test_diff_with_one_argument_runs_from_there_to_the_current_snapshot():
    metadata = load(four_commit_table())

    base, target = diff_endpoints(metadata, '4471', None)

    assert (base['snapshot-id'], target['snapshot-id']) == (SNAP_TWO, SNAP_FOUR)


def test_diff_on_a_first_commit_refuses_rather_than_comparing_it_to_nothing(capsys):
    metadata = TableMetadata(document=metadata_document([snapshot(SNAP_ONE, BASE_MS)], current=SNAP_ONE), location=METADATA_URI)

    with pytest.raises(typer.Exit) as exc:
        diff_endpoints(metadata, None, None)

    assert exc.value.exit_code == 1
    assert 'first commit' in capsys.readouterr().out


def test_diff_names_the_commits_that_ran_between_the_two_snapshots():
    # This is what turns a row-count change into an explanation: two appends and an overwrite is
    # a different incident from one overwrite.
    metadata = load(four_commit_table())

    comparison = diff_snapshots(metadata, metadata.by_id(SNAP_ONE), metadata.by_id(SNAP_FOUR))

    assert comparison.linear is True
    assert [s['snapshot-id'] for s in comparison.path] == [SNAP_TWO, SNAP_THREE, SNAP_FOUR]


def test_diff_the_wrong_way_round_reports_that_the_two_are_not_on_one_line():
    # The counters still subtract, but calling that "what ran between them" would be a lie.
    metadata = load(four_commit_table())

    comparison = diff_snapshots(metadata, metadata.by_id(SNAP_FOUR), metadata.by_id(SNAP_ONE))

    assert comparison.linear is False
    assert comparison.path == []


def test_diff_of_a_snapshot_against_itself_is_linear_with_no_commits():
    metadata = load(four_commit_table())

    comparison = diff_snapshots(metadata, metadata.by_id(SNAP_TWO), metadata.by_id(SNAP_TWO))

    assert comparison.linear is True
    assert comparison.path == []


# --- file layout ------------------------------------------------------------------------------


def test_file_rows_walk_back_from_the_named_snapshot():
    metadata = load(four_commit_table())

    rows = file_rows(metadata, metadata.by_id(SNAP_THREE), 10)

    assert [row['snapshot_id'] for row in rows] == [SNAP_THREE, SNAP_TWO, SNAP_ONE]
    assert rows[0]['data_files'] == 11
    assert rows[0]['average_file_bytes'] == 11534336 // 11


def test_file_rows_honour_the_limit():
    metadata = load(four_commit_table())

    assert len(file_rows(metadata, metadata.by_id(SNAP_FOUR), 2)) == 2


# --- the --json shape -------------------------------------------------------------------------


def test_snapshot_json_carries_integers_and_the_refs_pointing_at_it():
    metadata = load(four_commit_table())

    data = snapshot_to_dict(metadata, metadata.by_id(SNAP_THREE))

    assert data['snapshot_id'] == SNAP_THREE
    assert data['parent_snapshot_id'] == SNAP_TWO
    assert data['operation'] == 'overwrite'
    assert data['total_records'] == 1800
    assert data['deleted_records'] == 1200
    assert data['schema_id'] == 1
    assert data['refs'] == []
    # The raw summary travels too, because writers add keys of their own to it.
    assert data['summary']['operation'] == 'overwrite'


def test_snapshot_json_names_the_refs_that_point_at_a_snapshot():
    metadata = load(four_commit_table())

    assert snapshot_to_dict(metadata, metadata.by_id(SNAP_ONE))['refs'] == ['month-end']


# --- rendering --------------------------------------------------------------------------------


def test_the_file_table_hides_the_delete_column_on_a_copy_on_write_table():
    # Most tables never write a delete file, and a column of blanks crowds the numbers that moved.
    metadata = load(four_commit_table())

    with console.capture() as capture:
        render_files_table('events', file_rows(metadata, metadata.by_id(SNAP_FOUR), 10))

    assert 'delete files' not in capture.get()


def test_the_file_table_shows_the_delete_column_once_a_commit_reports_deletes():
    document = metadata_document(
        [snapshot(SNAP_ONE, BASE_MS, total_data_files=6, total_delete_files=2, total_position_deletes=340, total_files_size=6291456)],
        current=SNAP_ONE,
    )
    metadata = TableMetadata(document=document, location=METADATA_URI)

    with console.capture() as capture:
        render_files_table('events', file_rows(metadata, metadata.by_id(SNAP_ONE), 10))

    out = capture.get()
    assert 'delete files' in out
    assert '2' in out


def test_the_diff_counter_table_title_does_not_carry_two_snapshot_ids():
    # Two 19-digit ids wrap the title across the narrow table, and the lines above it already
    # name both ends of the comparison.
    metadata = load(four_commit_table())

    with console.capture() as capture:
        render_diff('events', diff_snapshots(metadata, metadata.by_id(SNAP_THREE), metadata.by_id(SNAP_FOUR)))

    assert 'events counters' in capture.get()


def test_the_snapshots_table_does_not_truncate_the_id():
    # The tail is the handle every other verb accepts, and an ellipsis would hide exactly it.
    metadata = load(four_commit_table())

    with console.capture() as capture:
        render_snapshots_table('events', metadata, [metadata.by_id(SNAP_TWO)])

    assert str(SNAP_TWO) in capture.get()


# --- the command surface ----------------------------------------------------------------------


def test_the_table_exposes_the_five_read_verbs(monkeypatch):
    app, _, _ = make_app(four_commit_table(), monkeypatch)

    result = runner.invoke(app, ['events', '--help'])

    assert result.exit_code == 0
    for verb in ('snapshots', 'history', 'files', 'diff', 'branches'):
        assert verb in result.stdout


def test_no_maintenance_verb_is_advertised(monkeypatch):
    # compact, expire, orphans and rollback are writes against a real estate whose mechanism is
    # unsettled. A verb in the help that is not built is worse than an absent one.
    app, _, _ = make_app(four_commit_table(), monkeypatch)

    result = runner.invoke(app, ['events', '--help'])

    for absent in ('compact', 'expire', 'orphans', 'rollback'):
        assert absent not in result.stdout


def test_an_unknown_table_alias_is_a_usage_error(monkeypatch):
    # The alias is a sub-app, so an unknown table is an unknown command rather than a resolve
    # failure, and --help at the resource level lists the ones that exist.
    app, _, _ = make_app(four_commit_table(), monkeypatch)

    assert runner.invoke(app, ['nope', 'snapshots']).exit_code != 0


def test_snapshots_json_is_a_clean_machine_readable_list(monkeypatch):
    app, _, _ = make_app(four_commit_table(), monkeypatch)

    result = runner.invoke(app, ['events', 'snapshots', '--json'])

    assert result.exit_code == 0
    data = json.loads(result.stdout)
    assert [row['snapshot_id'] for row in data] == [SNAP_FOUR, SNAP_THREE, SNAP_TWO, SNAP_ONE]


def test_snapshots_limit_trims_to_the_newest(monkeypatch):
    app, _, _ = make_app(four_commit_table(), monkeypatch)

    result = runner.invoke(app, ['events', 'snapshots', '--limit', '2', '--json'])

    assert [row['snapshot_id'] for row in json.loads(result.stdout)] == [SNAP_FOUR, SNAP_THREE]


def test_history_json_marks_the_abandoned_commits(monkeypatch):
    app, _, _ = make_app(rolled_back_table(), monkeypatch)

    result = runner.invoke(app, ['events', 'history', '--json'])

    assert result.exit_code == 0
    rows = json.loads(result.stdout)
    assert [row['snapshot_id'] for row in rows if not row['is_current_ancestor']] == [SNAP_THREE, SNAP_FOUR]


def test_branches_json_lists_every_ref(monkeypatch):
    app, _, _ = make_app(four_commit_table(), monkeypatch)

    result = runner.invoke(app, ['events', 'branches', '--json'])

    assert result.exit_code == 0
    assert [row['name'] for row in json.loads(result.stdout)] == ['main', 'nightly', 'month-end']


def test_files_json_reports_the_layout_at_each_commit(monkeypatch):
    app, _, _ = make_app(four_commit_table(), monkeypatch)

    result = runner.invoke(app, ['events', 'files', '--limit', '1', '--json'])

    assert result.exit_code == 0
    row = json.loads(result.stdout)[0]
    assert row['data_files'] == 14
    assert row['average_file_bytes'] == 12582912 // 14


def test_files_help_says_there_is_no_row_per_data_file(monkeypatch):
    # The manifests naming individual files are Avro, which dectl does not read. Someone reaching
    # for a file listing should learn that from the help rather than from the output.
    app, _, _ = make_app(four_commit_table(), monkeypatch)

    result = runner.invoke(app, ['events', 'files', '--help'])

    assert 'no row-per-file view' in result.stdout.replace('\n', ' ')


def test_diff_json_carries_the_commits_and_the_counter_changes(monkeypatch):
    app, _, _ = make_app(four_commit_table(), monkeypatch)

    result = runner.invoke(app, ['events', 'diff', '1234', '--json'])

    assert result.exit_code == 0
    data = json.loads(result.stdout)
    assert data['linear'] is True
    assert [row['snapshot_id'] for row in data['commits']] == [SNAP_TWO, SNAP_THREE, SNAP_FOUR]
    records = next(row for row in data['counters'] if row['key'] == 'total-records')
    assert (records['before'], records['after'], records['change']) == (1200, 2000, 800)
    assert data['schema_changed'] is True


def test_diff_reports_the_overwrite_that_dropped_rows(monkeypatch):
    # The incident this resource exists for: the row count fell yesterday, and the commit that
    # did it is named without opening a console.
    app, _, _ = make_app(four_commit_table(), monkeypatch)

    result = runner.invoke(app, ['events', 'diff', '4471', '9108', '--json'])

    assert result.exit_code == 0
    data = json.loads(result.stdout)
    records = next(row for row in data['counters'] if row['key'] == 'total-records')
    assert records['change'] == -600
    assert [row['operation'] for row in data['commits']] == ['overwrite']


def test_diff_the_wrong_way_round_still_answers_and_says_it_is_not_a_line(monkeypatch):
    app, _, _ = make_app(four_commit_table(), monkeypatch)

    result = runner.invoke(app, ['events', 'diff', '5567', '1234', '--json'])

    assert result.exit_code == 0
    data = json.loads(result.stdout)
    assert data['linear'] is False
    assert data['commits'] == []


def test_a_failure_to_read_the_table_exits_non_zero_through_the_command(monkeypatch):
    config = make_config({'events': {'database': 'lakehouse', 'table': 'events'}})
    monkeypatch.setattr('dectl.commands.iceberg.make_session', lambda _config: FakeSession(FakeGlue(), FakeS3()))
    app = make_iceberg_app('proj', config.pipelines['proj'], config)

    result = runner.invoke(app, ['events', 'snapshots'])

    assert result.exit_code == 1


def test_the_active_env_substitutes_into_the_database_and_table(monkeypatch):
    monkeypatch.setattr(active_environment, 'name', 'prod')
    document = four_commit_table()
    uri = 's3://example-lake/warehouse/events/metadata/prod.json'
    glue = FakeGlue({'salesdata-prod-catalog.events-prod': iceberg_table_entry(uri)})
    s3 = FakeS3({uri: json.dumps(document)})
    config = make_config({'events': {'database': 'salesdata-{env}-catalog', 'table': 'events-{env}'}})
    monkeypatch.setattr('dectl.commands.iceberg.make_session', lambda _config: FakeSession(glue, s3))
    app = make_iceberg_app('proj', config.pipelines['proj'], config)

    result = runner.invoke(app, ['events', 'branches', '--json'])

    assert result.exit_code == 0
    assert glue.calls == [{'DatabaseName': 'salesdata-prod-catalog', 'Name': 'events-prod'}]
