# tests/test_spill.py
from __future__ import annotations

import ast
from pathlib import Path

from imp.agents.tools import limits
from imp.agents.tools.context import ToolContext
from imp.agents.tools.spill import spill_tool_output, wrap_task_result


def _ctx(tmp_path: Path) -> ToolContext:
    run_dir = tmp_path / ".imp" / "quests" / "test-quest"
    run_dir.mkdir(parents=True, exist_ok=True)
    return ToolContext(project_root=tmp_path,
                       events_path=run_dir / "events.jsonl")


def test_preview_bytes_default_is_2000() -> None:
    assert limits.PREVIEW_BYTES == 2_000


def test_spill_writes_full_content_to_tool_results(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    spill_tool_output(ctx, "abc.txt", "hello world")
    spilled = tmp_path / ".imp" / "quests" / "test-quest" / "tool-results" / "abc.txt"
    assert spilled.read_text(encoding="utf-8") == "hello world"


def test_notice_carries_project_root_relative_path(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    result = spill_tool_output(ctx, "abc.txt", "x" * 60_000, preview_bytes=0)
    # relpath is from project_root (tmp_path), NOT absolute
    assert ".imp/quests/test-quest/tool-results/abc.txt" in result
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


def test_result_at_the_cap_is_returned_inline(tmp_path: Path) -> None:
    # AC 7: the trigger is strictly ">", so a result of exactly the cap is handed
    # back byte-for-byte and nothing is written to disk.
    ctx = _ctx(tmp_path)
    exact = "x" * 30_000
    assert wrap_task_result(ctx, "t1", exact) == exact
    assert not (tmp_path / ".imp" / "quests" / "test-quest" / "tool-results").exists()


def test_result_one_byte_over_the_cap_spills_with_a_2kb_preview(tmp_path: Path) -> None:
    # AC 7: one byte over -> a 2 KB head plus the existing notice verbatim, and the
    # complete result recoverable byte-for-byte from the named file.
    ctx = _ctx(tmp_path)
    over = "x" * 30_001
    preview, notice = wrap_task_result(ctx, "t1", over).split("\n", 1)
    assert len(preview.encode("utf-8")) == 2_000
    assert notice == ("[full output 30 KB at "
                      ".imp/quests/test-quest/tool-results/t1; "
                      "read with read_file offset=1]")
    spilled = tmp_path / ".imp" / "quests" / "test-quest" / "tool-results" / "t1"
    assert spilled.read_text(encoding="utf-8") == over


def test_over_cap_preview_never_splits_a_multibyte_char(tmp_path: Path) -> None:
    # AC 8: byte 2000 of this result lands mid-"中" (1999 ASCII then 3-byte chars).
    # A char-count slice would overrun 2000 bytes; a raw byte slice would emit
    # U+FFFD. The preview must be a valid, byte-bounded prefix of the full result.
    ctx = _ctx(tmp_path)
    result = "a" * 1_999 + "中" * 10_500          # 33_499 bytes -> over the cap
    preview = wrap_task_result(ctx, "t1", result).split("\n", 1)[0]
    assert "�" not in preview            # no replacement char
    assert len(preview.encode("utf-8")) <= 2_000
    assert result.encode("utf-8").startswith(preview.encode("utf-8"))
    assert preview == "a" * 1_999                 # the partial char is dropped


def _dispatch_task_call_sites() -> list[tuple[str, str, int, bool]]:
    """(rel_module, func_name, call_lineno, has_wrap) for each dispatch_task call site."""
    def _is_call(node: ast.AST, name: str) -> bool:
        return (isinstance(node, ast.Call) and
                ((isinstance(node.func, ast.Name) and node.func.id == name) or
                 (isinstance(node.func, ast.Attribute) and node.func.attr == name)))

    def _enclosing_func(
        tree: ast.AST, lineno: int
    ) -> ast.FunctionDef | ast.AsyncFunctionDef | None:
        best = None
        for node in ast.walk(tree):
            if (isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                    and node.lineno <= lineno <= node.end_lineno  # type: ignore[attr-defined]
                    and (best is None or node.lineno > best.lineno)):
                best = node
        return best

    src = Path(__file__).resolve().parents[1] / "src" / "imp"
    out: list[tuple[str, str, int, bool]] = []
    for path in sorted(src.rglob("*.py")):
        tree = ast.parse(path.read_text())
        rel = str(path.relative_to(src))
        for node in ast.walk(tree):
            if _is_call(node, "dispatch_task"):
                enc = _enclosing_func(tree, node.lineno)  # type: ignore[arg-type]
                if enc is None:
                    out.append((rel, "<module>", node.lineno, False))
                else:
                    has_wrap = any(_is_call(n, "wrap_task_result") for n in ast.walk(enc))
                    out.append((rel, enc.name, node.lineno, has_wrap))
    return out


def test_every_dispatch_task_call_site_wraps_its_result() -> None:
    # AC 9: each dispatch_task call site must have wrap_task_result in its own enclosing
    # function. Asserted over the DISCOVERED set; the floor below proves the scan sees all
    # three dispatch paths (anthropic/openai, codex, claude_code).
    sites = _dispatch_task_call_sites()
    assert {rel for rel, _, _, _ in sites} >= {
        "agents/dispatch.py", "providers/codex_mcp.py", "providers/claude.py"
    }
    for rel, func, lineno, has_wrap in sites:
        assert has_wrap, (
            f"{rel}: dispatch_task at line {lineno} in `{func}` has no wrap_task_result")
