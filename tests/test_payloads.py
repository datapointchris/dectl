import io

import pytest
import typer

from dectl.payloads import read_payload


def test_read_payload_defaults_to_empty_object():
    assert read_payload(None) == '{}'


def test_read_payload_reads_a_file(tmp_path):
    path = tmp_path / 'event.json'
    path.write_text('{"key": "value"}')
    assert read_payload(str(path)) == '{"key": "value"}'


def test_read_payload_reads_stdin_on_dash(monkeypatch):
    monkeypatch.setattr('sys.stdin', io.StringIO('{"from": "stdin"}'))
    assert read_payload('-') == '{"from": "stdin"}'


def test_read_payload_missing_file_exits(tmp_path):
    with pytest.raises(typer.Exit):
        read_payload(str(tmp_path / 'nope.json'))
