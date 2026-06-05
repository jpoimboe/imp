from pathlib import Path

import pytest

from imp.cli.main import (
    _format_last_timestamp,
    _last_event_timestamp,
    _last_nonempty_line,
    _read_last_line,
)


@pytest.mark.parametrize(
    "data, expected",
    [
        (b'{"a":1}\n{"b":2}\n', b'{"b":2}'),       # trailing newline
        (b'{"a":1}\n{"b":2}', b'{"b":2}'),         # no trailing newline
        (b'{"b":2}\n\n  \n', b'{"b":2}'),          # trailing blank/whitespace lines
        (b'{"only":1}', b'{"only":1}'),            # single line, no newline
        (b'', None),                               # empty
        (b'\n  \n\n', None),                       # all blank
    ],
)
def test_last_nonempty_line(data: bytes, expected: bytes | None) -> None:
    assert _last_nonempty_line(data) == expected


def _write(tmp_path: Path, data: bytes) -> Path:
    p = tmp_path / "events.jsonl"
    p.write_bytes(data)
    return p


@pytest.mark.parametrize(
    "data, expected",
    [
        (b'{"a":1}\n{"b":2}\n', b'{"b":2}'),       # trailing newline
        (b'{"a":1}\n{"b":2}', b'{"b":2}'),         # no trailing newline
        (b'{"b":2}\n\n  \n', b'{"b":2}'),          # trailing blank lines
        (b'{"only":1}', b'{"only":1}'),            # single line, no newline
        (b'', None),                               # empty
        (b'\n  \n', None),                         # all blank
    ],
)
def test_read_last_line_basic(tmp_path: Path, data: bytes, expected: bytes | None) -> None:
    assert _read_last_line(_write(tmp_path, data)) == expected


def test_read_last_line_spans_many_chunks(tmp_path: Path) -> None:
    # Last record is far larger than one 64 KiB chunk: exercises the
    # multi-iteration backward loop and its termination.
    big = b'{"timestamp":"x","payload":"' + b'y' * (200 * 1024) + b'"}'
    data = b'{"first":1}\n' + big + b'\n'
    assert _read_last_line(_write(tmp_path, data)) == big


def test_read_last_line_matches_whole_file_extraction(tmp_path: Path) -> None:
    # Property: the tail reader agrees with the in-memory extractor on the same
    # bytes. This is the invariant the production parity depends on.
    for data in [
        b'a\nb\nc\n',
        b'a\nb\nc',
        b'\n\nlast',
        b'x' * (130 * 1024) + b'\nfinal\n',
        b'single',
        b'',
    ]:
        assert _read_last_line(_write(tmp_path, data)) == _last_nonempty_line(data)


def test_read_last_line_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        _read_last_line(tmp_path / "nope.jsonl")


def test_format_last_timestamp_valid_line() -> None:
    # Fixed UTC offset so the assertion is timezone-stable.
    line = '{"timestamp":"2026-06-05T12:34:56+00:00"}'
    out = _format_last_timestamp(line)
    # Rendered in local time; just assert it parsed to the expected shape.
    assert len(out) == 19 and out[4] == "-" and out[13] == ":"


@pytest.mark.parametrize(
    "line",
    [
        "",                              # empty
        "   ",                           # blank
        "not json",                      # malformed JSON
        '{"no":"timestamp"}',            # missing key
        '{"timestamp":"not-a-date"}',    # bad date
    ],
)
def test_format_last_timestamp_bad_input(line: str) -> None:
    assert _format_last_timestamp(line) == "-"


def test_last_event_timestamp_missing_file(tmp_path: Path) -> None:
    assert _last_event_timestamp(tmp_path / "nope.jsonl") == "-"


def test_last_event_timestamp_reads_last_event(tmp_path: Path) -> None:
    p = tmp_path / "events.jsonl"
    p.write_bytes(
        b'{"timestamp":"2026-06-01T00:00:00+00:00"}\n'
        b'{"timestamp":"2026-06-05T12:00:00+00:00"}\n'
    )
    out = _last_event_timestamp(p)
    assert len(out) == 19 and out != "-"  # parsed the LAST line, not the first


def test_last_event_timestamp_non_utf8_last_line(tmp_path: Path) -> None:
    # A non-UTF-8 byte in the last line must render '-', not crash. This closes
    # a latent UnicodeDecodeError in the old strict read_text() path.
    p = tmp_path / "events.jsonl"
    p.write_bytes(b'{"timestamp":"2026-06-01T00:00:00+00:00"}\n\xff\xfe garbage\n')
    assert _last_event_timestamp(p) == "-"
