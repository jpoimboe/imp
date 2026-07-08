# tests/test_spill.py
from __future__ import annotations

from pathlib import Path

from imp.agents.tools import limits
from imp.agents.tools.context import ToolContext
from imp.agents.tools.spill import spill_tool_output


def _ctx(tmp_path: Path) -> ToolContext:
    run_dir = tmp_path / ".imp" / "change"
    run_dir.mkdir(parents=True, exist_ok=True)
    return ToolContext(project_root=tmp_path,
                       events_path=run_dir / "events.jsonl")


def test_preview_bytes_default_is_2000() -> None:
    assert limits.PREVIEW_BYTES == 2_000


def test_spill_writes_full_content_to_tool_results(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    spill_tool_output(ctx, "abc.txt", "hello world")
    spilled = tmp_path / ".imp" / "change" / "tool-results" / "abc.txt"
    assert spilled.read_text(encoding="utf-8") == "hello world"


def test_notice_carries_project_root_relative_path(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    result = spill_tool_output(ctx, "abc.txt", "x" * 60_000, preview_bytes=0)
    # relpath is from project_root (tmp_path), NOT absolute
    assert ".imp/change/tool-results/abc.txt" in result
    assert str(tmp_path) not in result
    assert "read with read_file offset=1" in result


def test_notice_reports_full_size_in_kb(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    result = spill_tool_output(ctx, "abc.txt", "x" * 60_000, preview_bytes=0)
    assert "60 KB" in result


def test_preview_bytes_zero_returns_notice_alone(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    result = spill_tool_output(ctx, "abc.txt", "hello world " * 100,
                               preview_bytes=0)
    assert result.startswith("[full output")
    assert "hello world" not in result


def test_default_preview_length_is_2000_bytes(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    result = spill_tool_output(ctx, "abc.txt", "a" * 5_000)
    preview = result.split("\n", 1)[0]
    assert len(preview) == 2_000


def test_preview_is_byte_safe_at_multibyte_boundary(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    # "abc" (3 bytes) + "中" (3 bytes: e4 b8 ad) + "def"; cut at 4 bytes lands
    # mid-"中". A byte-safe slice drops the partial char -> "abc".
    result = spill_tool_output(ctx, "abc.txt", "abc中def", preview_bytes=4)
    preview = result.split("\n", 1)[0]
    assert preview == "abc"


def test_preview_equals_full_when_shorter_than_preview_bytes(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    result = spill_tool_output(ctx, "abc.txt", "short output")
    preview = result.split("\n", 1)[0]
    assert preview == "short output"
