# tests/test_edit_file_ergonomics.py
"""edit_file ergonomics (tool-ergonomics spec): bounded strict-decode read,
replace_all, atomic write-back. Return/refusal texts are pinned."""
from __future__ import annotations

import os
from pathlib import Path

import anyio
import pytest

import imp.agents.tools.fs as fs_tools  # noqa: F401  triggers registration
from imp.agents.tools import get_registry
from imp.agents.tools.context import ToolContext
from imp.agents.tools.limits import FILE_READ_CAP_BYTES


def _ctx(tmp_path: Path) -> ToolContext:
    ctx = ToolContext(
        project_root=tmp_path,
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


def test_replace_all_replaces_every_occurrence(tmp_path: Path) -> None:
    (tmp_path / "x.py").write_text("a = 1\na = 1\na = 1\n")
    out = _edit(_ctx(tmp_path), path="x.py", old="a = 1", new="b = 2",
                replace_all=True)
    assert out == "replaced 3 occurrences in x.py"  # pinned return skeleton
    assert (tmp_path / "x.py").read_text() == "b = 2\nb = 2\nb = 2\n"


def test_replace_all_absent_old_still_raises(tmp_path: Path) -> None:
    (tmp_path / "x.py").write_text("nothing\n")
    with pytest.raises(ValueError, match="not found"):
        _edit(_ctx(tmp_path), path="x.py", old="missing", new="x",
              replace_all=True)


def test_default_mode_still_rejects_non_unique(tmp_path: Path) -> None:
    (tmp_path / "x.py").write_text("a\na\n")
    with pytest.raises(ValueError, match="not unique"):
        _edit(_ctx(tmp_path), path="x.py", old="a", new="b")


@pytest.mark.parametrize("replace_all", [False, True])
def test_empty_old_rejected_file_untouched(tmp_path: Path, replace_all: bool) -> None:
    # "abc".count("") == len+1: an empty old is never "absent", and under
    # replace_all the literal rule would insert `new` between EVERY character
    # of the file — a catastrophic rewrite from a degenerate argument.
    p = tmp_path / "x.py"
    p.write_text("content\n")
    with pytest.raises(ValueError, match="old must not be empty"):
        _edit(_ctx(tmp_path), path="x.py", old="", new="EVIL",
              replace_all=replace_all)
    assert p.read_text() == "content\n"


def test_exec_bit_survives_edit(tmp_path: Path) -> None:
    # mkstemp creates 0600; a bare temp+replace would silently strip a
    # script's executable bit. (Ownership/xattrs/ACLs are waived as accepted.)
    p = tmp_path / "run.sh"
    p.write_text("#!/bin/sh\necho hi\n")
    p.chmod(0o755)
    _edit(_ctx(tmp_path), path="run.sh", old="echo hi", new="echo bye")
    assert (p.stat().st_mode & 0o777) == 0o755
    assert p.read_text() == "#!/bin/sh\necho bye\n"


def test_no_stray_temp_files_after_edit(tmp_path: Path) -> None:
    (tmp_path / "x.py").write_text("a = 1\n")
    _edit(_ctx(tmp_path), path="x.py", old="a = 1", new="a = 2")
    # mkstemp names start with "tmp" — none may survive the os.replace.
    assert [f.name for f in tmp_path.iterdir() if f.name.startswith("tmp")] == []


def test_failed_replace_leaves_original_intact(tmp_path: Path,
                                               monkeypatch: pytest.MonkeyPatch) -> None:
    # Crash mid-write-back must not leave a truncated target — the original
    # survives untouched and the temp file is cleaned up.
    import imp.agents.tools.fs as fs

    def boom(src: object, dst: object) -> None:
        raise OSError("simulated replace failure")

    (tmp_path / "x.py").write_text("a = 1\n")
    monkeypatch.setattr(fs.os, "replace", boom)
    with pytest.raises(OSError, match="simulated replace failure"):
        _edit(_ctx(tmp_path), path="x.py", old="a = 1", new="a = 2")
    assert (tmp_path / "x.py").read_text() == "a = 1\n"
    leftovers = [f.name for f in tmp_path.iterdir()
                 if f.name not in ("x.py", "events.jsonl")]
    assert leftovers == []


def test_replace_all_single_occurrence_message_pinned(tmp_path: Path) -> None:
    # The spec pins f"replaced {count} occurrences" under replace_all — for
    # count=1 that is "replaced 1 occurrences", deliberately. A grammar "fix"
    # is a spec change.
    (tmp_path / "x.py").write_text("a = 1\n")
    out = _edit(_ctx(tmp_path), path="x.py", old="a = 1", new="b = 2",
                replace_all=True)
    assert out == "replaced 1 occurrences in x.py"


@pytest.mark.skipif(os.geteuid() == 0, reason="root bypasses directory permissions")
def test_readonly_directory_refused_with_curated_error(tmp_path: Path) -> None:
    # mkstemp in a non-writable directory must not leak a raw temp path; the
    # error names the user's file and the recovery, on-pattern with the
    # module's other refusals.
    d = tmp_path / "ro"
    d.mkdir()
    p = d / "x.py"
    p.write_text("a = 1\n")
    d.chmod(0o555)
    try:
        with pytest.raises(
            PermissionError, match=r"directory of ro/x\.py is read-only for this step"
        ):
            _edit(_ctx(tmp_path), path="ro/x.py", old="a = 1", new="a = 2")
        assert p.read_text() == "a = 1\n"
    finally:
        d.chmod(0o755)  # let pytest clean tmp_path up


def test_long_old_capped_in_not_found_message(tmp_path: Path) -> None:
    # A multi-KB `old` must not be echoed in full into the transcript (the
    # agent already has it in context as the call it just made): the repr is
    # capped at 200 chars with the total length disclosed.
    (tmp_path / "x.py").write_text("content\n")
    with pytest.raises(ValueError) as exc:
        _edit(_ctx(tmp_path), path="x.py", old="A" * 5000, new="y")
    msg = str(exc.value)
    assert "not found" in msg
    assert "[5000 chars total]" in msg
    assert "A" * 201 not in msg
    assert len(msg) < 300


def test_long_old_capped_in_not_unique_message(tmp_path: Path) -> None:
    long_old = "B" * 1000
    (tmp_path / "x.py").write_text(long_old + "x" + long_old)
    with pytest.raises(ValueError) as exc:
        _edit(_ctx(tmp_path), path="x.py", old=long_old, new="y")
    msg = str(exc.value)
    assert "not unique" in msg
    assert "(found 2 occurrences)" in msg
    assert "[1000 chars total]" in msg
    assert len(msg) < 300


def test_short_old_repr_stays_full_and_escaped(tmp_path: Path) -> None:
    # The repr's escaping is the valuable part (whitespace mistakes are the
    # top not-found cause) — short olds keep today's exact full-repr message.
    (tmp_path / "x.py").write_text("content\n")
    with pytest.raises(ValueError, match=r"'nope\\nx' not found in 'x\.py'"):
        _edit(_ctx(tmp_path), path="x.py", old="nope\nx", new="y")
