# tests/test_grep_ergonomics.py
"""grep ergonomics (tool-ergonomics spec): \n-split matching semantics,
deterministic traversal, output modes, glob filter, context, head_limit.
Notice/sentinel strings are NORMATIVE — asserted verbatim."""
from __future__ import annotations

import os
from pathlib import Path

import anyio
import pytest

import imp.agents.tools.fs  # side-effect: triggers tool registration
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
    assert n <= 9999  # m{i:04d} stays 4 digits — row_len arithmetic depends on it
    assert note_len > row_len  # first note must be what crosses, not a row
    for i in range(n):  # rows total < cap; rows + first note >= cap
        (tmp_path / f"m{i:04d}.txt").write_text("needle\n")
    out = _grep(_ctx(tmp_path), pattern="needle")
    lines = out.split("\n")
    assert lines[-1] == "... [output truncated at 50 KB] ..."
    assert "[a_trunc1.txt: scanned first 5 MB only]" in out
    assert "a_trunc2.txt" not in out  # its note was dropped under the cap


# --- glob filter -------------------------------------------------------------

def test_glob_star_matches_any_depth(tmp_path: Path) -> None:
    # fnmatch semantics: * crosses /, so *.py matches at ANY depth (CC's
    # "match anywhere" intent) — unlike the glob TOOL's Path.glob semantics.
    (tmp_path / "a.py").write_text("needle\n")
    (tmp_path / "d").mkdir()
    (tmp_path / "d" / "e.py").write_text("needle\n")
    (tmp_path / "d" / "f.txt").write_text("needle\n")
    out = _grep(_ctx(tmp_path), pattern="needle", glob="*.py")
    assert out == "a.py\nd/e.py"


def test_glob_doublestar_nested_and_zero_depth(tmp_path: Path) -> None:
    # fnmatch's **/ must consume a /; the variant expansion restores the
    # zero-or-more-directories meaning: src/**/*.ts matches BOTH.
    (tmp_path / "src" / "a").mkdir(parents=True)
    (tmp_path / "src" / "a" / "b.ts").write_text("needle\n")
    (tmp_path / "src" / "x.ts").write_text("needle\n")
    out = _grep(_ctx(tmp_path), pattern="needle", glob="src/**/*.ts")
    # Walk order: a directory's own files come before its subdirs' files.
    assert out == "src/x.ts\nsrc/a/b.ts"


def test_glob_mixed_depth_multi_doublestar(tmp_path: Path) -> None:
    # The discriminating shape: first **/ non-zero-depth, second zero-depth,
    # with the literal fixtures/ anchoring the pattern. The rejected
    # two-variant implementation (all-kept + all-deleted only) misses this
    # file. (src/fixtures/a/b.ts would NOT discriminate — the all-deleted
    # variant matches it via the slash-crossing trailing *.)
    p = tmp_path / "src" / "a" / "fixtures"
    p.mkdir(parents=True)
    (p / "b.ts").write_text("needle\n")
    out = _grep(_ctx(tmp_path), pattern="needle", glob="src/**/fixtures/**/*.ts")
    assert out == "src/a/fixtures/b.ts"


def test_glob_empty_or_whitespace_rejected(tmp_path: Path) -> None:
    # fnmatchcase would silently match nothing; "(no matches)" must not hide
    # that the FILTER was the problem.
    (tmp_path / "a.py").write_text("x\n")
    with pytest.raises(ValueError, match="glob pattern must not be empty"):
        _grep(_ctx(tmp_path), pattern="x", glob="")
    with pytest.raises(ValueError, match="glob pattern must not be empty"):
        _grep(_ctx(tmp_path), pattern="x", glob="   ")


def test_glob_too_many_doublestar_occurrences(tmp_path: Path) -> None:
    # Model-generated input: bound the 2^n expansion at 5 occurrences.
    (tmp_path / "a.py").write_text("x\n")
    with pytest.raises(ValueError,
                       match=r"glob pattern has too many \*\*/ occurrences \(max 5\)"):
        _grep(_ctx(tmp_path), pattern="x", glob="**/" * 6 + "*.py")


def test_glob_filters_single_file_path_uniformly(tmp_path: Path) -> None:
    # One anchoring base regardless of `path`: a single-file path that fails
    # the glob filter is filtered out, not exempted.
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "x.py").write_text("needle\n")
    out = _grep(_ctx(tmp_path), pattern="needle", path="src/x.py", glob="*.ts")
    assert out == "(no matches)"


def test_glob_stays_case_sensitive_under_case_insensitive(tmp_path: Path) -> None:
    # case_insensitive applies to the REGEX only; the glob filter is always
    # case-sensitive (fnmatchcase, no normcase).
    (tmp_path / "a.md").write_text("needle\n")
    out = _grep(_ctx(tmp_path), pattern="NEEDLE", case_insensitive=True, glob="*.MD")
    assert out == "(no matches)"


def test_single_file_path_bypasses_excluded_dirs(tmp_path: Path) -> None:
    # Pre-existing behavior, owned and KEPT (not a drive-by fix): an explicit
    # single-file path reaches files the walk never would — explicit path is
    # explicit intent.
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "config").write_text("SECRET\n")
    out = _grep(_ctx(tmp_path), pattern="SECRET", path=".git/config")
    assert out == ".git/config"


def test_glob_variants_unit() -> None:
    from imp.agents.tools.nav import _glob_variants

    assert _glob_variants("src/**/*.ts") == ["src/**/*.ts", "src/*.ts"]
    # Literal non-overlapping count: ***/ contains one **/ (at offset 1);
    # mid-segment a**/b counts too (fnmatch has no segment concept).
    assert _glob_variants("a**/b") == ["a**/b", "ab"]
    assert len(_glob_variants("src/**/f/**/*.ts")) == 4


def test_glob_filtered_truncated_file_gets_no_note_and_no_read(tmp_path: Path) -> None:
    # The glob filter sits BEFORE the read: a filtered-out cap-truncated file
    # never pays the 5 MB read, never computes cap-truncation, and must not
    # produce a scan note (it was excluded by the agent's own filter — the
    # false-negative disclosure rationale doesn't apply).
    _write_cap_cut(tmp_path, "big.log")
    (tmp_path / "a.py").write_text("needle\n")
    out = _grep(_ctx(tmp_path), pattern="needle", glob="*.py")
    assert out == "a.py"
    assert "scanned first 5 MB" not in out


# --- context lines (content mode only) ---------------------------------------

def test_context_row_formats_match_colon_context_dash(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("one\ntwo\nNEEDLE\nfour\nfive\n")
    out = _grep(_ctx(tmp_path), pattern="NEEDLE", output_mode="content", context=1)
    assert out == "a.txt-2-two\na.txt:3:NEEDLE\na.txt-4-four"


def test_before_after_asymmetric(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("one\ntwo\nNEEDLE\nfour\nfive\n")
    out = _grep(_ctx(tmp_path), pattern="NEEDLE", output_mode="content",
                before=2, after=1)
    assert out == "a.txt-1-one\na.txt-2-two\na.txt:3:NEEDLE\na.txt-4-four"


def test_context_combined_with_before_uses_max(tmp_path: Path) -> None:
    # Order-free precedence (right for a JSON call, unlike GNU grep's
    # last-flag-wins): effective per-side = max(before, context).
    (tmp_path / "a.txt").write_text("a\nb\nc\nNEEDLE\ne\nf\ng\n")
    out = _grep(_ctx(tmp_path), pattern="NEEDLE", output_mode="content",
                context=3, before=1)
    assert out == ("a.txt-1-a\na.txt-2-b\na.txt-3-c\na.txt:4:NEEDLE\n"
                   "a.txt-5-e\na.txt-6-f\na.txt-7-g")


def test_context_clamps_at_file_edges(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("NEEDLE\nlast\n")
    out = _grep(_ctx(tmp_path), pattern="NEEDLE", output_mode="content", before=3)
    assert out == "a.txt:1:NEEDLE"


def test_overlapping_and_adjacent_blocks_merge_without_separator(tmp_path: Path) -> None:
    # Matches at 2 and 4 with context=1: ranges [1,3] and [3,5] overlap ->
    # ONE block, no -- inside it.
    (tmp_path / "a.txt").write_text("a\nNEEDLE\nc\nNEEDLE\ne\n")
    out = _grep(_ctx(tmp_path), pattern="NEEDLE", output_mode="content", context=1)
    assert out == "a.txt-1-a\na.txt:2:NEEDLE\na.txt-3-c\na.txt:4:NEEDLE\na.txt-5-e"
    assert "--" not in out


def test_distinct_blocks_separated_by_dashes(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("NEEDLE\nb\nc\nd\nNEEDLE\n")
    out = _grep(_ctx(tmp_path), pattern="NEEDLE", output_mode="content", context=1)
    assert out == ("a.txt:1:NEEDLE\na.txt-2-b\n--\n"
                   "a.txt-4-d\na.txt:5:NEEDLE")


def test_blocks_separated_across_file_boundaries(tmp_path: Path) -> None:
    # GNU grep behavior: the last block of file A and the first of file B get
    # a -- between them; blocks never merge across files.
    (tmp_path / "a.txt").write_text("NEEDLE\n")
    (tmp_path / "b.txt").write_text("NEEDLE\n")
    out = _grep(_ctx(tmp_path), pattern="NEEDLE", output_mode="content", context=1)
    assert out == "a.txt:1:NEEDLE\n--\nb.txt:1:NEEDLE"


def test_zero_context_is_bare_row_stream_no_separators(tmp_path: Path) -> None:
    # With effective context 0 every match is its own "block", but a literal
    # reading would put -- between every pair of rows — zero-context content
    # mode stays the bare stream, exactly as today.
    (tmp_path / "a.txt").write_text("NEEDLE\nNEEDLE\nNEEDLE\n")
    out = _grep(_ctx(tmp_path), pattern="NEEDLE", output_mode="content")
    assert out == "a.txt:1:NEEDLE\na.txt:2:NEEDLE\na.txt:3:NEEDLE"


def test_context_rows_capped_at_line_cap(tmp_path: Path) -> None:
    # Today's code caps only MATCHING lines; an uncapped 2 MB minified
    # neighbor would eat the whole 50 KB budget as one context row.
    from imp.agents.tools.limits import MATCH_LINE_CAP_BYTES
    huge = "z" * (MATCH_LINE_CAP_BYTES + 100)
    (tmp_path / "a.txt").write_text(f"{huge}\nNEEDLE\n")
    out = _grep(_ctx(tmp_path), pattern="NEEDLE", output_mode="content", before=1)
    row1, row2 = out.split("\n")
    assert row1 == f"a.txt-1-{'z' * MATCH_LINE_CAP_BYTES}…[truncated]"
    assert row2 == "a.txt:2:NEEDLE"


def test_nonzero_context_requires_content_mode(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("x\n")
    with pytest.raises(ValueError,
                       match="context/before/after require output_mode='content'"):
        _grep(_ctx(tmp_path), pattern="x", context=2)
    # Pinned to NONZERO: the body can't distinguish an explicit before=0 from
    # an omitted one (async_invoke calls fn(ctx, **args)) — harmless 0 accepted.
    assert _grep(_ctx(tmp_path), pattern="x", before=0) == "a.txt"


def test_negative_context_rejected(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("x\n")
    with pytest.raises(ValueError, match="must be >= 0"):
        _grep(_ctx(tmp_path), pattern="x", output_mode="content", before=-1)


def test_cap_cut_fragment_never_an_after_context_row(tmp_path: Path) -> None:
    # Match on the LAST complete line of a cap-truncated file with after=1:
    # the dropped fragment must not be pulled in as a context row.
    p = tmp_path / "cut.txt"
    with p.open("wb") as f:
        f.write(b"x\n" * 2_499_994)   # 4,999,988 bytes
        f.write(b"NEEDLE\n")          # bytes 4,999,989..4,999,995
        f.write(b"zfoo!")             # 5 bytes -> cap lands exactly at 5,000,000
        f.write(b"rest\n" * 100)      # continues past the cap
    assert p.stat().st_size > FILE_READ_CAP_BYTES
    out = _grep(_ctx(tmp_path), pattern="NEEDLE", output_mode="content", after=1)
    assert out == ("cut.txt:2499995:NEEDLE\n"
                   "[cut.txt: scanned first 5 MB only]")


def test_exactly_adjacent_blocks_merge(tmp_path: Path) -> None:
    # The exactly-adjacent boundary: ranges [1,3] and [4,6] touch via
    # lo == prev_hi + 1 — ONE block, no -- (the most regression-prone
    # character in the merge predicate is that + 1).
    (tmp_path / "a.txt").write_text("NEEDLE\nb\nc\nNEEDLE\ne\nf\ng\n")
    out = _grep(_ctx(tmp_path), pattern="NEEDLE", output_mode="content", after=2)
    assert out == ("a.txt:1:NEEDLE\na.txt-2-b\na.txt-3-c\n"
                   "a.txt:4:NEEDLE\na.txt-5-e\na.txt-6-f")
    assert "--" not in out


# --- head_limit ---------------------------------------------------------------

_HEAD_NOTICE = ("[further results omitted: head_limit={n} reached "
                "— raise head_limit or narrow the pattern]")


def test_head_limit_fwm_stops_scanning_with_notice(tmp_path: Path) -> None:
    for name in ("a.txt", "b.txt", "c.txt", "d.txt"):
        (tmp_path / name).write_text("needle\n")
    out = _grep(_ctx(tmp_path), pattern="needle", head_limit=2)
    # Sorted walk order makes WHICH two files deterministic.
    assert out == f"a.txt\nb.txt\n{_HEAD_NOTICE.format(n=2)}"


def test_head_limit_count_mode(tmp_path: Path) -> None:
    for name in ("a.txt", "b.txt", "c.txt"):
        (tmp_path / name).write_text("needle\n")
    out = _grep(_ctx(tmp_path), pattern="needle", output_mode="count", head_limit=1)
    assert out == f"a.txt:1\n{_HEAD_NOTICE.format(n=1)}"


def test_head_limit_counts_match_rows_only_not_context_or_separators(
        tmp_path: Path) -> None:
    # Three DISTINCT blocks (matches at 2, 6, 10 with context=1 — ranges
    # [1,3], [5,7], [9,11] don't touch, so no merging): head_limit=2 counts
    # the 2 MATCH rows; the 4 context rows and the -- don't count against it.
    (tmp_path / "a.txt").write_text("a\nNEEDLE\nc\nd\ne\nNEEDLE\ng\nh\ni\nNEEDLE\nk\n")
    out = _grep(_ctx(tmp_path), pattern="NEEDLE", output_mode="content",
                context=1, head_limit=2)
    assert out == ("a.txt-1-a\na.txt:2:NEEDLE\na.txt-3-c\n--\n"
                   "a.txt-5-e\na.txt:6:NEEDLE\na.txt-7-g\n"
                   + _HEAD_NOTICE.format(n=2))


def test_head_limit_merged_block_emitted_whole_overshooting(tmp_path: Path) -> None:
    # The cap is a stopping condition checked BETWEEN blocks; a merged block
    # holding more matches than the remaining budget is emitted whole (the
    # merge invariant wins — row count overshoots head_limit).
    (tmp_path / "a.txt").write_text("NEEDLE\nb\nNEEDLE\nd\ne\nf\nNEEDLE\n")
    out = _grep(_ctx(tmp_path), pattern="NEEDLE", output_mode="content",
                context=1, head_limit=1)
    # Matches at 1 and 3 merge (ranges [1,2] and [2,4]) -> both emitted; the
    # distinct block at 7 is cut.
    assert out == ("a.txt:1:NEEDLE\na.txt-2-b\na.txt:3:NEEDLE\na.txt-4-d\n"
                   + _HEAD_NOTICE.format(n=1))


def test_byte_backstop_may_cut_mid_block_with_distinct_marker(tmp_path: Path) -> None:
    # head_limit never cuts a block; the 50 KB byte backstop CAN and DOES (it
    # yields to nothing). Its marker text is distinct from the head_limit
    # notice — the agent's recovery differs.
    line = "y" * 1_900
    body = "".join(f"{line} NEEDLE\n" for _ in range(100))
    (tmp_path / "big.txt").write_text(body)
    out = _grep(_ctx(tmp_path), pattern="NEEDLE", output_mode="content", context=2)
    assert "[output truncated at 50 KB]" in out
    assert "further results omitted" not in out


def test_head_limit_exit_keeps_collected_notes(tmp_path: Path) -> None:
    # head_limit early exit does NOT drop notes collected up to the stop.
    # 'a_trunc.txt' sorts before the limit-hitting matches.
    _write_cap_cut(tmp_path, "a_trunc.txt")  # no match in scanned window
    (tmp_path / "m1.txt").write_text("needle\n")
    (tmp_path / "m2.txt").write_text("needle\n")
    out = _grep(_ctx(tmp_path), pattern="needle", head_limit=1)
    assert out == ("m1.txt\n[a_trunc.txt: scanned first 5 MB only]\n"
                   + _HEAD_NOTICE.format(n=1))


def test_head_limit_below_one_rejected(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("x\n")
    with pytest.raises(ValueError, match="head_limit must be >= 1"):
        _grep(_ctx(tmp_path), pattern="x", head_limit=0)


def test_head_limit_content_exact_landing_stops_scan_and_notices(tmp_path: Path) -> None:
    # The limit lands exactly on a.txt's last match: the scan must stop —
    # a truncated NON-matching file sorting later must not be read (its scan
    # note must not leak), and the notice must still be emitted (vacuous-
    # notice semantics, same as fwm).
    (tmp_path / "a.txt").write_text("NEEDLE\n")
    _write_cap_cut(tmp_path, "z_trunc.txt")  # sorts after; no match in window
    out = _grep(_ctx(tmp_path), pattern="NEEDLE", output_mode="content",
                head_limit=1)
    assert out == ("a.txt:1:NEEDLE\n"
                   + _HEAD_NOTICE.format(n=1))


def test_byte_cap_wins_over_head_limit_when_both_fire(tmp_path: Path) -> None:
    # The row that reaches head_limit also crosses the 50 KB budget: the
    # byte marker (physical cut — stronger disclosure) wins; never two
    # markers.
    #
    # Arithmetic: line = "y" * 1_900; each row = "big.txt:{i}:{line} NEEDLE"
    # Row lengths: rows 1-9 are "big.txt:N:..." (8+1900+8=1917 chars + 1 join) = 1918 bytes
    # rows 10-27 are "big.txt:NN:..." (9+1900+8=1917 chars + 1 join) = 1919 bytes
    # Cumulative at row 26: 9*1918 + 17*1919 = 17262 + 32623 = 49885 bytes (< 50000)
    # Cumulative at row 27: 49885 + 1919 = 51804 bytes (>= 50000) -> cap fires at row 27
    # So head_limit=27 means the 27th row is BOTH the head_limit boundary AND the
    # budget-crossing row.
    line = "y" * 1_900
    body = "".join(f"{line} NEEDLE\n" for _ in range(40))
    (tmp_path / "big.txt").write_text(body)
    out = _grep(_ctx(tmp_path), pattern="NEEDLE", output_mode="content",
                head_limit=27)  # row 27 is the budget-crossing row (see arithmetic above)
    assert "[output truncated at 50 KB]" in out
    assert "further results omitted" not in out


# --- cross-tool claims --------------------------------------------------------

def _read_ctx(tmp_path: Path) -> ToolContext:
    from imp.engine.types import FsRule, SourceLoc
    ctx = ToolContext(
        project_root=tmp_path,
        fs_permissions=(FsRule(
            operations=("read",), paths=("**",), mode="allow",
            source=SourceLoc(file=Path("t.yaml"), line=1),
        ),),
        events_path=tmp_path / "events.jsonl",
    )
    ctx.coordinator = None
    return ctx


def test_files_with_matches_rows_round_trip_into_read_file_and_grep(
        tmp_path: Path) -> None:
    # The machine-consumable claim, exercised: a files_with_matches row feeds
    # straight back into read_file and grep path= (this is why scan notes
    # must never be appended to a path row).
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "deep.py").write_text("needle here\n")
    row = _grep(_ctx(tmp_path), pattern="needle")
    assert row == "src/deep.py"

    read_out = anyio.run(get_registry()["read_file"].async_invoke,
                         _read_ctx(tmp_path), {"path": row})
    assert read_out == "     1\tneedle here"

    again = _grep(_ctx(tmp_path), pattern="needle", path=row,
                  output_mode="content")
    assert again == "src/deep.py:1:needle here"


def test_grep_and_read_file_agree_on_formfeed_line_numbers(tmp_path: Path) -> None:
    # Cross-tool alignment — the whole point of \n-splitting both tools: on a
    # form-feed file, grep's reported lineno N is read_file's rendered line N
    # (splitlines() would have called NEEDLE line 3 in one tool only).
    (tmp_path / "ff.txt").write_text("a\fb\nNEEDLE\nc\n")
    g = _grep(_ctx(tmp_path), pattern="NEEDLE", output_mode="content")
    assert g == "ff.txt:2:NEEDLE"
    r = anyio.run(get_registry()["read_file"].async_invoke,
                  _read_ctx(tmp_path), {"path": "ff.txt"})
    assert r.split("\n")[1] == "     2\tNEEDLE"


def test_fifo_inside_walked_tree_is_skipped_not_hung(tmp_path: Path) -> None:
    # os.walk yields fifos in filenames; grep opening one read-only blocks
    # until a writer appears — in a worker thread, BEFORE any tool_checkpoint
    # and immune to cancellation (anyio to_thread does not abandon), so no
    # in-process timeout can rescue it. The walk itself must skip non-regular
    # files. Tested at the _iter_files level: a grep-level test would HANG
    # the suite on regression instead of failing. (An EXPLICIT fifo path
    # still errors loudly — pinned above.)
    from imp.agents.tools.nav import _iter_files

    (tmp_path / "sub").mkdir()
    os.mkfifo(tmp_path / "sub" / "pipe")
    (tmp_path / "sub" / "real.txt").write_text("needle\n")

    assert [p.name for p in _iter_files(tmp_path)] == ["real.txt"]

    # And end-to-end (safe once the walk skips it): the fifo neither hangs
    # nor matches, the regular file is found.
    out = _grep(_ctx(tmp_path), pattern="needle")
    assert out == "sub/real.txt"
