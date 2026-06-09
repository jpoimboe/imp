# tests/test_edit_file_ergonomics.py
"""edit_file ergonomics (tool-ergonomics spec): bounded strict-decode read,
replace_all, atomic write-back. Return/refusal texts are pinned."""
from __future__ import annotations

from pathlib import Path

import anyio
import pytest

import imp.agents.tools.fs as fs_tools  # noqa: F401  triggers registration
from imp.agents.tools import get_registry
from imp.agents.tools.context import ToolContext
from imp.agents.tools.limits import FILE_READ_CAP_BYTES
from imp.engine.types import FsRule, SourceLoc


def _ctx(tmp_path: Path) -> ToolContext:
    ctx = ToolContext(
        project_root=tmp_path,
        fs_permissions=(FsRule(
            operations=("read", "write"), paths=("**",), mode="allow",
            source=SourceLoc(file=Path("t.yaml"), line=1),
        ),),
        events_path=tmp_path / "events.jsonl",
    )
    ctx.coordinator = None
    return ctx


def _edit(ctx: ToolContext, **args: object) -> str:
    return anyio.run(get_registry()["edit_file"].async_invoke, ctx, args)


def test_basic_edit_still_works_and_message_pinned(tmp_path: Path) -> None:
    (tmp_path / "x.py").write_text("def foo():\n    return 1\n")
    out = _edit(_ctx(tmp_path), path="x.py", old="return 1", new="return 2")
    assert out == "replaced 1 occurrence in x.py"
    assert (tmp_path / "x.py").read_text() == "def foo():\n    return 2\n"


def test_over_cap_file_refused(tmp_path: Path) -> None:
    p = tmp_path / "big.txt"
    p.write_bytes((b"a" * 99 + b"\n") * 51_000)  # 5,100,000 bytes > cap
    before = p.read_bytes()
    with pytest.raises(ValueError, match="larger than 5 MB"):
        _edit(_ctx(tmp_path), path="big.txt", old="a", new="b")
    assert p.read_bytes() == before  # file untouched


def test_exactly_5mb_file_edits_successfully(tmp_path: Path) -> None:
    # st_size disambiguation: the reader says truncated=True for an
    # exactly-at-cap file, but it was read completely — editable.
    n = FILE_READ_CAP_BYTES // 100
    p = tmp_path / "big.txt"
    p.write_bytes(b"NEEDLE" + b"x" * 93 + b"\n" + (b"x" * 99 + b"\n") * (n - 1))
    assert p.stat().st_size == FILE_READ_CAP_BYTES
    out = _edit(_ctx(tmp_path), path="big.txt", old="NEEDLE", new="NOODLE")
    assert out == "replaced 1 occurrence in big.txt"
    assert p.read_bytes().startswith(b"NOODLE")
    assert p.stat().st_size == FILE_READ_CAP_BYTES


def test_refusal_order_size_before_decode(tmp_path: Path) -> None:
    # A >5 MB file of PURE valid UTF-8 whose byte cap lands mid-multibyte
    # sequence (99-byte lines of 2-byte é: 5,000,000 % 99 == 5 -> the cut
    # splits a sequence). Decode-first would fabricate a "not valid UTF-8"
    # refusal about a valid file; the size check must win.
    p = tmp_path / "big_utf8.txt"
    p.write_bytes((b"\xc3\xa9" * 49 + b"\n") * 51_000)  # valid UTF-8, > 5 MB
    with pytest.raises(ValueError, match="larger than 5 MB"):
        _edit(_ctx(tmp_path), path="big_utf8.txt", old="é", new="e")


def test_non_utf8_file_refused_never_u_fffd_rewrite(tmp_path: Path) -> None:
    # A lone \xff OUTSIDE the edited region: the file is either byte-identical
    # after the call or the edit is refused — never rewritten with U+FFFD.
    p = tmp_path / "latin.py"
    p.write_bytes(b"x = 1\n# caf\xff comment\n")
    before = p.read_bytes()
    with pytest.raises(ValueError, match="not valid UTF-8"):
        _edit(_ctx(tmp_path), path="latin.py", old="x = 1", new="x = 2")
    assert p.read_bytes() == before


def test_crlf_round_trip_preserved(tmp_path: Path) -> None:
    # Deliberate side-fix: the old read_text() was universal-newlines and
    # mangled CRLF on write-back; the bytes path preserves them.
    p = tmp_path / "win.txt"
    p.write_bytes(b"alpha\r\nbeta\r\n")
    _edit(_ctx(tmp_path), path="win.txt", old="alpha", new="ALPHA")
    assert p.read_bytes() == b"ALPHA\r\nbeta\r\n"


def test_missing_path_error_text(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match=r"no such file: nope\.py"):
        _edit(_ctx(tmp_path), path="nope.py", old="a", new="b")


def test_directory_error_text(tmp_path: Path) -> None:
    (tmp_path / "d").mkdir()
    with pytest.raises(IsADirectoryError, match="is a directory: d"):
        _edit(_ctx(tmp_path), path="d", old="a", new="b")


def test_vanished_after_truncated_read_refuses(tmp_path: Path,
                                               monkeypatch: pytest.MonkeyPatch) -> None:
    # truncated=True + stat failure -> refuse (writing back the first 5 MB of
    # a possibly-larger file would truncate it).
    import imp.agents.tools.fs as fs

    async def fake_read(path: Path, **kw: object) -> tuple[bytes, bool]:
        path.unlink()
        return (b"data", True)

    (tmp_path / "v.txt").write_text("data")
    monkeypatch.setattr(fs, "read_bytes_capped", fake_read)
    with pytest.raises(ValueError, match="larger than 5 MB"):
        _edit(_ctx(tmp_path), path="v.txt", old="data", new="x")
