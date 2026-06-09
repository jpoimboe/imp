# tests/test_grep_ergonomics.py
"""grep ergonomics (tool-ergonomics spec): \n-split matching semantics,
deterministic traversal, output modes, glob filter, context, head_limit.
Notice/sentinel strings are NORMATIVE — asserted verbatim."""
from __future__ import annotations

import os
from pathlib import Path

import anyio
import pytest

import imp.agents.tools.nav  # noqa: F401  triggers registration
from imp.agents.tools import get_registry
from imp.agents.tools.context import ToolContext
from imp.agents.tools.limits import FILE_READ_CAP_BYTES, TOOL_OUTPUT_CAP_BYTES


def _ctx(tmp_path: Path) -> ToolContext:
    ctx = ToolContext(
        project_root=tmp_path,
        fs_permissions=(),
        events_path=tmp_path / "events.jsonl",
    )
    ctx.coordinator = None
    return ctx


def _grep(ctx: ToolContext, **args: object) -> str:
    return anyio.run(get_registry()["grep"].async_invoke, ctx, args)


def _write_cap_cut(tmp_path: Path, name: str = "cut.txt") -> Path:
    """A file whose first 5 MB ends exactly with the fragment 'zfoo' mid-line,
    and which continues past the cap (so it IS cap-truncated)."""
    p = tmp_path / name
    with p.open("wb") as f:
        f.write(b"x\n" * 2_499_998)   # 4,999,996 bytes
        f.write(b"zfoo")              # bytes 4,999,997..5,000,000: cut lands after 'foo'
        f.write(b"barbar\n" * 100)    # the file continues beyond the cap
    assert p.stat().st_size > FILE_READ_CAP_BYTES
    return p


# --- \n-split matching semantics -------------------------------------------

def test_crlf_dollar_anchor_owned_regression(tmp_path: Path) -> None:
    # \n-split leaves the \r in the regex input (GNU grep/ripgrep/CC parity;
    # today's splitlines() imp is the outlier). foo$ must NOT match foo\r;
    # foo\r?$ must.
    (tmp_path / "crlf.txt").write_bytes(b"foo\r\nbar\r\n")
    assert _grep(_ctx(tmp_path), pattern="foo$") == "(no matches)"
    out = _grep(_ctx(tmp_path), pattern="foo\\r?$")
    assert out.startswith("crlf.txt")


def test_phantom_trailing_element_never_matched(tmp_path: Path) -> None:
    # "a\nb\n".split("\n") yields a trailing "" that is NOT a line; feeding it
    # to the regex would let ^$ report a match at line N+1 of every
    # newline-terminated file.
    (tmp_path / "t.txt").write_text("a\nb\n")
    assert _grep(_ctx(tmp_path), pattern="^$") == "(no matches)"


def test_cap_cut_fragment_never_matched_and_sentinel_coexists_with_note(
        tmp_path: Path) -> None:
    # The partial trailing element of a >5 MB read is never fed to the regex
    # (foo$ would false-positive on the cut at '...zfoo'). AND: zero result
    # rows + a truncated-scan note emits the sentinel FIRST, then the note —
    # the note must not suppress the sentinel.
    _write_cap_cut(tmp_path)
    out = _grep(_ctx(tmp_path), pattern="foo$")
    assert out == "(no matches)\n[cut.txt: scanned first 5 MB only]"


# --- deterministic traversal + path errors ----------------------------------

def test_walk_order_is_sorted_and_deterministic(tmp_path: Path) -> None:
    # Sorted dirnames/filenames in the walk. NOTE: topdown walks yield root
    # files before subdir files (this is NOT sorted(all_paths)).
    #
    # Discrimination strategy: create files in non-alphabetical inode order so
    # an unsorted walk would return them in creation order (m, a, z, t, g, f).
    # Also create two sibling subdirs in non-alphabetical creation order (y
    # before b) each with a file, so an unsorted subdir walk would visit y/
    # before b/. The test then asserts the full sorted ordering.
    #
    # Root files created in non-alpha order (m before a before z etc.):
    for name in ["m.txt", "a.txt", "z.txt", "t.txt", "g.txt", "f.txt"]:
        (tmp_path / name).write_text("needle\n")
    # Subdirs created non-alphabetically: y before b
    (tmp_path / "y").mkdir()
    (tmp_path / "y" / "yw.txt").write_text("needle\n")
    (tmp_path / "b").mkdir()
    (tmp_path / "b" / "bc.txt").write_text("needle\n")

    out = _grep(_ctx(tmp_path), pattern="needle")

    # Root files must appear before any subdir file (topdown property):
    for root_file in ["a.txt", "f.txt", "g.txt", "m.txt", "t.txt", "z.txt"]:
        assert out.index(root_file) < out.index("b/"), (
            f"root file {root_file!r} should appear before subdir b/"
        )
        assert out.index(root_file) < out.index("y/"), (
            f"root file {root_file!r} should appear before subdir y/"
        )

    # Root files must be in sorted (alphabetical) order among themselves:
    assert out.index("a.txt") < out.index("f.txt"), "a.txt before f.txt"
    assert out.index("f.txt") < out.index("g.txt"), "f.txt before g.txt"
    assert out.index("g.txt") < out.index("m.txt"), "g.txt before m.txt"
    assert out.index("m.txt") < out.index("t.txt"), "m.txt before t.txt"
    assert out.index("t.txt") < out.index("z.txt"), "t.txt before z.txt"

    # Subdirs must be visited in sorted (alphabetical) order: b/ before y/:
    assert out.index("b/") < out.index("y/"), "subdir b/ before y/"


def test_nonexistent_path_is_an_error_not_no_matches(tmp_path: Path) -> None:
    # A typo'd or stale path must not read as "searched, nothing found".
    with pytest.raises(FileNotFoundError, match="no such path: nope"):
        _grep(_ctx(tmp_path), pattern="x", path="nope")


def test_fifo_path_is_an_error_not_no_matches(tmp_path: Path) -> None:
    # Neither a regular file nor a directory: would otherwise fall through
    # _iter_files to a silent (no matches).
    os.mkfifo(tmp_path / "pipe")
    with pytest.raises(FileNotFoundError, match="no such path: pipe"):
        _grep(_ctx(tmp_path), pattern="x", path="pipe")


# --- output modes ------------------------------------------------------------

def test_default_mode_is_files_with_matches(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("hello\nhello\n")
    (tmp_path / "b.py").write_text("nothing\n")
    assert _grep(_ctx(tmp_path), pattern="hello") == "a.py"  # paths only, once


def test_content_mode_rows(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("hay\nneedle\n")
    out = _grep(_ctx(tmp_path), pattern="needle", output_mode="content")
    assert out == "a.py:2:needle"


def test_count_mode_emits_only_nonzero_rows(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("x\nx\nx\n")
    (tmp_path / "b.py").write_text("nothing\n")
    out = _grep(_ctx(tmp_path), pattern="x", output_mode="count")
    assert out == "a.py:3"  # no b.py:0 flood (ripgrep -c, not GNU grep)


def test_count_mode_all_zero_is_no_matches_sentinel(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("nothing\n")
    out = _grep(_ctx(tmp_path), pattern="absent", output_mode="count")
    assert out == "(no matches)"


def test_content_mode_zero_matches_sentinel(tmp_path: Path) -> None:
    # The sentinel is pinned in ALL THREE modes (per-mode drift is a bug);
    # files_with_matches and count are pinned elsewhere — this is content.
    (tmp_path / "a.py").write_text("nothing\n")
    out = _grep(_ctx(tmp_path), pattern="absent", output_mode="content")
    assert out == "(no matches)"


def test_case_insensitive_regex(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("HELLO World\n")
    assert _grep(_ctx(tmp_path), pattern="hello") == "(no matches)"
    assert _grep(_ctx(tmp_path), pattern="hello", case_insensitive=True) == "a.py"


def test_invalid_output_mode_rejected(tmp_path: Path) -> None:
    # The schema enum is only a suggestion to the model — the body rejects.
    (tmp_path / "a.py").write_text("x\n")
    with pytest.raises(ValueError, match="files_with_matches, content, count"):
        _grep(_ctx(tmp_path), pattern="x", output_mode="paths")


def test_files_with_matches_exact_walk_order(tmp_path: Path) -> None:
    (tmp_path / "z.txt").write_text("needle\n")
    (tmp_path / "b").mkdir()
    (tmp_path / "b" / "c.txt").write_text("needle\n")
    # Topdown walk: root files first — NOT sorted(all_paths).
    assert _grep(_ctx(tmp_path), pattern="needle") == "z.txt\nb/c.txt"


# --- scan notes per mode -----------------------------------------------------

def _write_cap_cut_with_needle(tmp_path: Path, name: str) -> Path:
    """Cap-truncated file whose scanned window CONTAINS a match."""
    p = tmp_path / name
    with p.open("wb") as f:
        f.write(b"NEEDLE\n")
        f.write(b"x\n" * 2_600_000)  # 5,200,000 bytes -> well past the cap
    assert p.stat().st_size > FILE_READ_CAP_BYTES
    return p


def test_fwm_matched_truncated_file_gets_no_note(tmp_path: Path) -> None:
    # A LISTED file resolved itself (stopped at its first match) — its note
    # would be vacuous. An UNLISTED truncated file is a potential false
    # negative — silence would hide it.
    _write_cap_cut_with_needle(tmp_path, "matched.txt")
    _write_cap_cut(tmp_path, "unmatched.txt")
    out = _grep(_ctx(tmp_path), pattern="NEEDLE")
    assert out == "matched.txt\n[unmatched.txt: scanned first 5 MB only]"


def test_count_mode_truncated_file_always_noted(tmp_path: Path) -> None:
    # In count mode the count is a lower bound — note every truncated file,
    # matched or not. Notes trail the result rows.
    _write_cap_cut_with_needle(tmp_path, "matched.txt")
    out = _grep(_ctx(tmp_path), pattern="NEEDLE", output_mode="count")
    assert out == "matched.txt:1\n[matched.txt: scanned first 5 MB only]"


def test_byte_cap_during_trailing_notes_drops_remainder_with_marker(
        tmp_path: Path) -> None:
    # Notes count toward the 50 KB budget; when the byte cap fires DURING
    # trailing-note emission the remaining notes are dropped — acceptable only
    # because the marker then discloses the physical cut. Self-tuning fixture:
    # size the result rows to sit just under the cap so the FIRST note crosses
    # it and the second is dropped.
    _write_cap_cut(tmp_path, "a_trunc1.txt")  # sorts first -> note buffered first
    _write_cap_cut(tmp_path, "a_trunc2.txt")
    note_len = len("[a_trunc1.txt: scanned first 5 MB only]") + 1
    row_len = len("m0000.txt") + 1
    n = (TOOL_OUTPUT_CAP_BYTES - note_len) // row_len + 1
    for i in range(n):  # rows total < cap; rows + first note >= cap
        (tmp_path / f"m{i:04d}.txt").write_text("needle\n")
    out = _grep(_ctx(tmp_path), pattern="needle")
    lines = out.split("\n")
    assert lines[-1] == "... [output truncated at 50 KB] ..."
    assert "[a_trunc1.txt: scanned first 5 MB only]" in out
    assert "a_trunc2.txt" not in out  # its note was dropped under the cap
