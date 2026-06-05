from pathlib import Path

import pytest

from imp.cli.main import _last_nonempty_line, _read_last_line


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
