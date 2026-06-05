import pytest

from imp.cli.main import _last_nonempty_line


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
