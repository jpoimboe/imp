"""imp/memory/ filesystem memory: comment stripping, snapshot loading, and
broad->narrow resolution (spec hede92 slice 1)."""
from __future__ import annotations

from pathlib import Path

from imp.providers.memory import (
    load_memory_snapshot,
    resolve_memory,
    strip_html_comments,
)


def test_strip_removes_single_line_comment() -> None:
    assert strip_html_comments("a<!-- drop me -->b") == "ab"


def test_strip_removes_multiline_comment() -> None:
    assert strip_html_comments("x<!--\nmany\nlines\n-->y") == "xy"


def test_strip_removes_multiple_comments() -> None:
    assert strip_html_comments("<!--a-->keep<!--b-->this") == "keepthis"


def test_strip_leaves_hash_headings_untouched() -> None:
    # '#' is a markdown heading, NOT a comment.
    assert strip_html_comments("# Heading\ntext") == "# Heading\ntext"


def test_strip_no_comment_is_identity() -> None:
    assert strip_html_comments("plain prose") == "plain prose"


def _seed_tree(root: Path) -> Path:
    mem = root / "memory"
    (mem / "imp").mkdir(parents=True)
    (mem / "all.md").write_text("GLOBAL PROSE")
    (mem / "imp" / "all.md").write_text("WORKFLOW PROSE")
    (mem / "imp" / "threshold.md").write_text("<!-- note -->STEP PROSE")
    (mem / "imp" / "empty.md").write_text("<!-- only a comment -->\n")
    return mem


def test_snapshot_absent_dir_is_empty(tmp_path: Path) -> None:
    assert load_memory_snapshot(tmp_path / "memory") == {}


def test_snapshot_keys_by_posix_relpath_with_stripped_content(tmp_path: Path) -> None:
    mem = _seed_tree(tmp_path)
    snap = load_memory_snapshot(mem)
    assert snap["all.md"] == "GLOBAL PROSE"
    assert snap["imp/all.md"] == "WORKFLOW PROSE"
    assert snap["imp/threshold.md"] == "STEP PROSE"  # HTML comment stripped


def test_snapshot_includes_comment_only_file_as_blank(tmp_path: Path) -> None:
    mem = _seed_tree(tmp_path)
    snap = load_memory_snapshot(mem)
    assert "imp/empty.md" in snap
    assert snap["imp/empty.md"].strip() == ""
