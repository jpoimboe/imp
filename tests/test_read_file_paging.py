# tests/test_read_file_paging.py
"""read_file paging/line-numbering (tool-ergonomics spec). Notice skeleton
strings are NORMATIVE — asserted verbatim; wording changes go through the
spec, not the implementation."""
from __future__ import annotations

from pathlib import Path

import anyio
import pytest

import imp.agents.tools.fs as fs_tools  # noqa: F401  triggers registration
from imp.agents.tools import get_registry
from imp.agents.tools.context import ToolContext
from imp.agents.tools.limits import (
    FILE_READ_CAP_BYTES,
    MATCH_LINE_CAP_BYTES,
    TOOL_OUTPUT_CAP_BYTES,
)


def _ctx(tmp_path: Path) -> ToolContext:
    ctx = ToolContext(
        project_root=tmp_path,
        events_path=tmp_path / "events.jsonl",
    )
    ctx.coordinator = None
    return ctx


def _read(ctx: ToolContext, **args: object) -> str:
    return anyio.run(get_registry()["read_file"].async_invoke, ctx, args)


def _num(n: int, content: str) -> str:
    # cat -n style: lineno right-aligned in a 6-column field before a tab.
    # Deliberately NOT asserted for >999,999-line files — the field widens
    # naturally there and the 6-column width is not universal.
    return f"{n:>6}\t{content}"


def test_basic_window_is_line_numbered(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("alpha\nbeta\ngamma\n")
    out = _read(_ctx(tmp_path), path="a.txt")
    # Complete file, window covers EOF: ends SILENTLY — no notice line.
    assert out == f"{_num(1, 'alpha')}\n{_num(2, 'beta')}\n{_num(3, 'gamma')}"


def test_offset_and_limit_slice_with_notice(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("".join(f"line{i}\n" for i in range(1, 11)))
    out = _read(_ctx(tmp_path), path="a.txt", offset=3, limit=2)
    assert out == (f"{_num(3, 'line3')}\n{_num(4, 'line4')}\n"
                   "[showing lines 3-4 of 10; continue with offset=5]")


def test_window_overshooting_eof_is_silent(tmp_path: Path) -> None:
    # offset + limit - 1 >= total (overshoot): NO notice at all. The >=
    # matters — an exact-fit == predicate would emit a bogus
    # continue-instruction here and invite a wasted empty-window call.
    (tmp_path / "a.txt").write_text("".join(f"l{i}\n" for i in range(1, 51)))
    out = _read(_ctx(tmp_path), path="a.txt", offset=10, limit=2000)
    assert out.endswith(_num(50, "l50"))
    assert "[" not in out


def test_window_exact_fit_at_eof_is_silent(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("a\nb\nc\n")
    out = _read(_ctx(tmp_path), path="a.txt", offset=2, limit=2)
    assert out == f"{_num(2, 'b')}\n{_num(3, 'c')}"


def test_unterminated_final_line_is_counted(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("a\nb")  # no trailing newline
    out = _read(_ctx(tmp_path), path="a.txt")
    assert out == f"{_num(1, 'a')}\n{_num(2, 'b')}"


def test_terminal_newline_phantom_not_counted(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("a\nb\n")
    out = _read(_ctx(tmp_path), path="a.txt")
    assert out == f"{_num(1, 'a')}\n{_num(2, 'b')}"  # 2 lines, never 3


def test_formfeed_numbers_by_newline_count(tmp_path: Path) -> None:
    # splitlines() would call this 4 lines; \n-counting says 2.
    (tmp_path / "ff.txt").write_text("a\fb\nc\vd")
    out = _read(_ctx(tmp_path), path="ff.txt")
    ff_line = "a\fb"
    vd_line = "c\vd"
    assert out == f"{_num(1, ff_line)}\n{_num(2, vd_line)}"


def test_empty_file_sentinel(tmp_path: Path) -> None:
    (tmp_path / "e.txt").write_text("")
    # Exactly the notice — never a bare empty string (indistinguishable from
    # a failed read).
    assert _read(_ctx(tmp_path), path="e.txt") == "[empty file]"


def test_empty_file_wins_over_past_eof(tmp_path: Path) -> None:
    (tmp_path / "e.txt").write_text("")
    assert _read(_ctx(tmp_path), path="e.txt", offset=5) == "[empty file]"


def test_offset_past_eof_notice(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("a\nb\nc\n")
    out = _read(_ctx(tmp_path), path="a.txt", offset=7)
    assert out == "[offset 7 is past the end of the file (3 lines)]"


def test_long_line_truncated_prefix_excluded_from_content_cap(tmp_path: Path) -> None:
    long_line = "y" * (MATCH_LINE_CAP_BYTES + 50)
    (tmp_path / "l.txt").write_text(long_line + "\nshort\n")
    out = _read(_ctx(tmp_path), path="l.txt")
    line1, line2 = out.split("\n")
    # Content capped at 2000 CHARS with grep's marker; the {lineno:>6}\t
    # prefix does NOT count toward the 2000 (it bounds what a line may
    # contribute; the 50 KB budget bounds what the agent receives).
    assert line1 == _num(1, "y" * MATCH_LINE_CAP_BYTES + "…[truncated]")
    assert line2 == _num(2, "short")


@pytest.mark.parametrize("bad", [0, -1, True, 1.0, "3"])
def test_invalid_offset_rejected(tmp_path: Path, bad: int) -> None:
    (tmp_path / "a.txt").write_text("x\n")
    with pytest.raises(ValueError, match="offset"):
        _read(_ctx(tmp_path), path="a.txt", offset=bad)


@pytest.mark.parametrize("bad", [0, -3, True, 2.5, "10"])
def test_invalid_limit_rejected(tmp_path: Path, bad: int) -> None:
    (tmp_path / "a.txt").write_text("x\n")
    with pytest.raises(ValueError, match="limit"):
        _read(_ctx(tmp_path), path="a.txt", limit=bad)


def test_missing_file_error_text(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match=r"no such file: nope\.txt"):
        _read(_ctx(tmp_path), path="nope.txt")


def test_directory_error_text(tmp_path: Path) -> None:
    (tmp_path / "d").mkdir()
    with pytest.raises(IsADirectoryError, match="is a directory: d"):
        _read(_ctx(tmp_path), path="d")


def test_output_budget_cutoff_renders_crossing_line_full_and_resumes(tmp_path: Path) -> None:
    # 3000 ~100-char lines: the 50 KB budget fires long before limit=2000.
    # Replicate the spec'd accounting (len(entry)+1 per rendered line, the
    # crossing line emitted in full, notice uncounted) to compute the expected
    # last line; then assert the verbatim notice and gap-free continuation.
    content = "".join(f"line {i:04d} " + "x" * 90 + "\n" for i in range(1, 3001))
    (tmp_path / "big.txt").write_text(content)
    lines = content.split("\n")[:-1]

    acc = 0
    expected_last = 0
    for i, line in enumerate(lines[:2000], start=1):
        acc += len(f"{i:>6}\t{line}") + 1
        expected_last = i
        if acc >= TOOL_OUTPUT_CAP_BYTES:
            break
    assert expected_last < 2000  # the BUDGET, not limit, is the limiter here

    out = _read(_ctx(tmp_path), path="big.txt", offset=1, limit=2000)
    body, notice = out.rsplit("\n", 1)
    # (a) the budget-crossing line is rendered IN FULL, not cut mid-line
    assert body.endswith(_num(expected_last, lines[expected_last - 1]))
    # (b) verbatim notice with the computed {next} value
    assert notice == (f"[showing lines 1-{expected_last} of 3000; "
                      f"continue with offset={expected_last + 1}]")

    # (c) offset = last + 1 is gap-free and overlap-free by construction
    out2 = _read(_ctx(tmp_path), path="big.txt", offset=expected_last + 1, limit=2000)
    assert out2.split("\n", 1)[0] == _num(expected_last + 1, lines[expected_last])


def _write_lines(tmp_path: Path, n_lines: int, line_len: int) -> Path:
    """n_lines lines of exactly line_len bytes each (content + newline)."""
    p = tmp_path / "big.txt"
    with p.open("wb") as f:
        f.write((b"x" * (line_len - 1) + b"\n") * n_lines)
    return p


def test_cap_truncated_more_readable_lines_notice(tmp_path: Path) -> None:
    # 99-byte lines: the 5 MB cap cuts mid-line (5,000,000 % 99 == 5), so the
    # trailing 5-byte fragment is dropped. Window ends via cutoff 1 (limit),
    # but cap-truncation is a FILE property: readable-window vocabulary, no
    # fabricated total.
    _write_lines(tmp_path, 51_000, 99)  # 5,049,000 bytes > 5 MB cap
    readable = FILE_READ_CAP_BYTES // 99
    out = _read(_ctx(tmp_path), path="big.txt", offset=1, limit=10)
    body, notice = out.rsplit("\n", 1)
    assert body.startswith(_num(1, "x" * 98))
    assert notice == (f"[showing lines 1-10 of the first 5 MB "
                      f"({readable} lines readable; total unknown); "
                      f"continue with offset=11]")


def test_cap_truncated_budget_cutoff_uses_readable_vocab(tmp_path: Path) -> None:
    # Cutoff 2 (50 KB budget) fires (~470 numbered 98-char lines) — vocabulary
    # must STILL be readable-window: the cutoff that fired doesn't matter.
    import re

    _write_lines(tmp_path, 51_000, 99)
    readable = FILE_READ_CAP_BYTES // 99
    out = _read(_ctx(tmp_path), path="big.txt", offset=1, limit=2000)
    notice = out.rsplit("\n", 1)[1]
    m = re.fullmatch(
        r"\[showing lines 1-(\d+) of the first 5 MB "
        rf"\({readable} lines readable; total unknown\); continue with offset=(\d+)\]",
        notice)
    assert m is not None, notice
    last = int(m.group(1))
    assert 1 < last < 2000
    assert int(m.group(2)) == last + 1


def test_end_of_readable_window_notice_exact_and_overshoot(tmp_path: Path) -> None:
    # A window ending at the last readable line must NOT end silently (quiet
    # endings claim completeness) — exact fit AND overshoot, same >= rule as
    # the normal-file notice predicate.
    _write_lines(tmp_path, 51_000, 99)
    readable = FILE_READ_CAP_BYTES // 99
    edge = ("[showing lines {first}-{last}; end of the readable window (first 5 MB) "
            "— the file continues but is not readable via read_file; total unknown]")

    out = _read(_ctx(tmp_path), path="big.txt", offset=readable - 9, limit=10)
    assert out.rsplit("\n", 1)[1] == edge.format(first=readable - 9, last=readable)

    out = _read(_ctx(tmp_path), path="big.txt", offset=readable - 9, limit=500)
    assert out.rsplit("\n", 1)[1] == edge.format(first=readable - 9, last=readable)


def test_offset_past_readable_window(tmp_path: Path) -> None:
    _write_lines(tmp_path, 51_000, 99)
    readable = FILE_READ_CAP_BYTES // 99
    out = _read(_ctx(tmp_path), path="big.txt", offset=readable + 1)
    assert out == (f"[offset {readable + 1} is past the readable window "
                   f"({readable} lines within the first 5 MB; total unknown)]")


def test_giant_single_line_file_has_zero_readable_lines(tmp_path: Path) -> None:
    # The incident shape itself: a >5 MB file with no \n in its first 5 MB.
    # The whole readable window is one partial fragment, which the drop rule
    # removes — readable == 0 and EVERY call lands past the readable window.
    # Pinned DELIBERATELY: today's tool showed the first 50 KB of such a file;
    # under the paging model it is wholly unreadable via read_file (use
    # bash/grep). A friendlier message is a spec change, not a plan deviation.
    p = tmp_path / "one_line.bin"
    p.write_bytes(b"z" * (FILE_READ_CAP_BYTES + 100))
    out = _read(_ctx(tmp_path), path="one_line.bin")
    assert out == ("[offset 1 is past the readable window "
                   "(0 lines within the first 5 MB; total unknown)]")


def test_exactly_5mb_file_is_complete(tmp_path: Path) -> None:
    # st_size disambiguation: reader says truncated=True, but nothing was
    # dropped — real final line, true total, normal-file silence at EOF.
    n = FILE_READ_CAP_BYTES // 100  # 50,000 lines of 100 bytes = exactly 5 MB
    p = _write_lines(tmp_path, n, 100)
    assert p.stat().st_size == FILE_READ_CAP_BYTES
    out = _read(_ctx(tmp_path), path="big.txt", offset=n - 1, limit=10)
    assert out == f"{_num(n - 1, 'x' * 99)}\n{_num(n, 'x' * 99)}"


def test_cap_on_newline_boundary_keeps_last_readable_line(tmp_path: Path) -> None:
    # The 5 MB cap lands exactly ON a \n; the trailing split element is the
    # phantom "" (already removed by the phantom rule) — a naive
    # drop-the-last-element would delete a complete, real final readable line
    # and shift every continuation number.
    n = FILE_READ_CAP_BYTES // 100
    p = tmp_path / "big.txt"
    with p.open("wb") as f:
        f.write((b"x" * 99 + b"\n") * n)   # exactly 5 MB, ends on \n
        f.write(b"beyond the cap\n" * 10)  # file continues -> cap-truncated
    out = _read(_ctx(tmp_path), path="big.txt", offset=n, limit=1)
    assert out.split("\n")[0] == _num(n, "x" * 99)
    assert "end of the readable window" in out


def test_vanished_file_stat_split(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # truncated=False + stat failure -> complete (silent normal render);
    # truncated=True + stat failure -> cap-truncated vocabulary, no total.
    import imp.agents.tools.fs as fs

    async def fake_read_complete(path: Path, **kw: object) -> tuple[bytes, bool]:
        path.unlink()  # vanish between read and the st_size sample
        return (b"a\nb\n", False)

    async def fake_read_truncated(path: Path, **kw: object) -> tuple[bytes, bool]:
        path.unlink()
        return (b"a\nb\npartial", True)

    (tmp_path / "v.txt").write_text("a\nb\n")
    monkeypatch.setattr(fs, "read_bytes_capped", fake_read_complete)
    ctx = _ctx(tmp_path)
    out = _read(ctx, path="v.txt")
    assert out == f"{_num(1, 'a')}\n{_num(2, 'b')}"  # complete: silent, not a flake
    assert ctx.read_state == {}  # vanished file leaves no record

    (tmp_path / "v.txt").write_text("a\nb\npartial")
    monkeypatch.setattr(fs, "read_bytes_capped", fake_read_truncated)
    ctx = _ctx(tmp_path)
    out = _read(ctx, path="v.txt")
    assert ctx.read_state == {}  # vanished file leaves no record
    # partial fragment dropped, window ends at the readable edge
    assert out.rsplit("\n", 1)[1] == (
        "[showing lines 1-2; end of the readable window (first 5 MB) "
        "— the file continues but is not readable via read_file; total unknown]")


def test_stub_on_exact_repeat_unchanged_file(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("".join(f"line{i}\n" for i in range(1, 11)))
    ctx = _ctx(tmp_path)
    first = _read(ctx, path="a.txt", offset=3, limit=2)
    assert first == (f"{_num(3, 'line3')}\n{_num(4, 'line4')}\n"
                     "[showing lines 3-4 of 10; continue with offset=5]")
    second = _read(ctx, path="a.txt", offset=3, limit=2)
    assert second == "[unchanged since you read it at lines 3-4 above]"


def test_stub_span_reflects_overshoot_not_requested_range(tmp_path: Path) -> None:
    # Window overshoots EOF: true last (3) != offset + limit - 1 (2000).
    (tmp_path / "a.txt").write_text("alpha\nbeta\ngamma\n")
    ctx = _ctx(tmp_path)
    _read(ctx, path="a.txt", offset=1, limit=2000)
    stub = _read(ctx, path="a.txt", offset=1, limit=2000)
    assert stub == "[unchanged since you read it at lines 1-3 above]"


def test_stub_span_reflects_budget_cutoff(tmp_path: Path) -> None:
    # The 50 KB budget ends the window before limit=2000; the stub must cite the
    # true rendered last line, not the requested range.
    content = "".join(f"line {i:04d} " + "x" * 90 + "\n" for i in range(1, 3001))
    (tmp_path / "big.txt").write_text(content)
    lines = content.split("\n")[:-1]
    acc = 0
    expected_last = 0
    for i, line in enumerate(lines[:2000], start=1):
        acc += len(f"{i:>6}\t{line}") + 1
        expected_last = i
        if acc >= TOOL_OUTPUT_CAP_BYTES:
            break
    assert expected_last < 2000  # budget, not limit, is the limiter here
    ctx = _ctx(tmp_path)
    _read(ctx, path="big.txt", offset=1, limit=2000)
    stub = _read(ctx, path="big.txt", offset=1, limit=2000)
    assert stub == f"[unchanged since you read it at lines 1-{expected_last} above]"


def test_mutation_invalidates_and_resets_ranges(tmp_path: Path) -> None:
    p = tmp_path / "a.txt"
    p.write_text("aaa\nbbb\nccc\n")
    ctx = _ctx(tmp_path)
    _read(ctx, path="a.txt", offset=1, limit=2000)
    p.write_text("xxx\nyyy\n")  # sha differs -> not the same version
    reread = _read(ctx, path="a.txt", offset=1, limit=2000)
    assert reread == f"{_num(1, 'xxx')}\n{_num(2, 'yyy')}"  # full new body, no stub
    assert "[unchanged" not in reread
    # ranges reset to the new version: a repeat now stubs with the NEW span
    stub = _read(ctx, path="a.txt", offset=1, limit=2000)
    assert stub == "[unchanged since you read it at lines 1-2 above]"


def test_different_range_never_stubs_then_each_repeat_stubs(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("".join(f"l{i}\n" for i in range(1, 101)))
    ctx = _ctx(tmp_path)
    first = _read(ctx, path="a.txt", offset=1, limit=50)
    assert first.startswith(_num(1, "l1"))
    assert "[unchanged" not in first
    second = _read(ctx, path="a.txt", offset=51, limit=50)  # different range
    assert second.startswith(_num(51, "l51"))
    assert "[unchanged" not in second
    # each remembered range now stubs on an exact repeat
    assert _read(ctx, path="a.txt", offset=1, limit=50) == (
        "[unchanged since you read it at lines 1-50 above]")
    assert _read(ctx, path="a.txt", offset=51, limit=50) == (
        "[unchanged since you read it at lines 51-100 above]")


def test_fresh_context_starts_with_empty_range_map(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("alpha\nbeta\ngamma\n")
    ctx1 = _ctx(tmp_path)
    _read(ctx1, path="a.txt", offset=1, limit=2000)
    ctx2 = _ctx(tmp_path)  # new ToolContext == fresh per-step state
    out = _read(ctx2, path="a.txt", offset=1, limit=2000)
    assert out == f"{_num(1, 'alpha')}\n{_num(2, 'beta')}\n{_num(3, 'gamma')}"
    assert "[unchanged" not in out
