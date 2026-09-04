"""Tool arguments through the real FastMCP validation path.

A hand-built args dict cannot reproduce the fabricated-None defect — the
generated ArgModel is what materialized it — which is why the unit suite
missed it.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from imp.agents.tools import Tool, get_registry
from imp.agents.tools.context import ToolContext
from imp.providers.codex_mcp import RunState
from tests._codex_mcp import call_via_mcp


def _ctx(tmp_path: Path) -> ToolContext:
    return ToolContext(
        project_root=tmp_path,
        events_path=tmp_path / "events.jsonl", subagent_defs={},
        provider_name="codex", model_name="gpt-5.5",
        parent_transcript_dir=tmp_path,
    )


def _spy(name: str, observed: dict[str, Any]) -> Tool:
    """The registered tool with its body wrapped, so a test can assert the exact
    kwargs the real function received — not merely that no error came back."""
    real = get_registry()[name]

    async def _fn(ctx: Any, **kwargs: Any) -> Any:
        observed.update(kwargs)
        return await real.fn(ctx, **kwargs)

    return Tool(name=real.name, description=real.description,
                input_schema=real.input_schema, fn=_fn, read_only=real.read_only)


def _call(name: str, tmp_path: Path, arguments: dict[str, Any]) -> tuple[Any, dict[str, Any]]:
    observed: dict[str, Any] = {}
    out = asyncio.run(call_via_mcp(
        _spy(name, observed), ctx=_ctx(tmp_path), state=RunState(cap=10),
        arguments=arguments))
    return out, observed


def test_read_file_omitting_offset_and_limit_reads_from_line_one(tmp_path: Path) -> None:
    (tmp_path / "f.txt").write_text("alpha\nbeta\ngamma\n")
    out, observed = _call("read_file", tmp_path, {"path": "f.txt"})
    assert out == "     1\talpha\n     2\tbeta\n     3\tgamma"
    assert observed == {"path": "f.txt"}


def test_read_file_explicit_null_offset_behaves_like_omitted(tmp_path: Path) -> None:
    (tmp_path / "f.txt").write_text("alpha\nbeta\ngamma\n")
    out, observed = _call(
        "read_file", tmp_path, {"path": "f.txt", "offset": None, "limit": None})
    assert out == "     1\talpha\n     2\tbeta\n     3\tgamma"
    assert observed == {"path": "f.txt"}


def test_grep_omitting_path_and_window_args_uses_declared_defaults(tmp_path: Path) -> None:
    (tmp_path / "f.txt").write_text("alpha\nbeta\n")
    out, observed = _call("grep", tmp_path, {"pattern": "beta"})
    assert out == "f.txt"  # path="." + output_mode="files_with_matches" applied
    assert observed == {"pattern": "beta"}


def test_grep_explicit_nulls_behave_like_omitted(tmp_path: Path) -> None:
    (tmp_path / "f.txt").write_text("alpha\nbeta\n")
    out, observed = _call("grep", tmp_path, {
        "pattern": "beta", "path": None, "before": None, "after": None,
        "context": None, "output_mode": None})
    assert out == "f.txt"
    assert observed == {"pattern": "beta"}


def test_symbol_outline_omitting_detail_applies_the_signatures_default(
    tmp_path: Path,
) -> None:
    (tmp_path / "m.py").write_text("def foo(a):\n    return a\n")
    out, observed = _call("symbol_outline", tmp_path, {"path": "m.py"})
    # `detail` never reached the body, so its declared default applied — and the
    # rendering really is the signatures one (counts renders without the symbol).
    assert observed == {"path": "m.py"}
    assert out == "m.py — 1 fn\n1: def foo(a):"
    counts, _ = _call("symbol_outline", tmp_path, {"path": "m.py", "detail": "counts"})
    assert counts == "m.py — 1 fn"


def test_symbol_outline_explicit_null_detail_behaves_like_omitted(tmp_path: Path) -> None:
    (tmp_path / "m.py").write_text("def foo(a):\n    return a\n")
    out, observed = _call(
        "symbol_outline", tmp_path, {"path": "m.py", "detail": None, "max_depth": None})
    assert observed == {"path": "m.py"}
    assert out == "m.py — 1 fn\n1: def foo(a):"


def test_required_arg_sent_as_null_is_not_dropped(tmp_path: Path) -> None:
    out, observed = _call("read_file", tmp_path, {"path": None})
    assert observed == {"path": None}  # required null reached the tool untouched
    # The tool's OWN error surfaced. Matched on the exception type only: the
    # message body is CPython's pathlib wording, not imp's to pin.
    assert "TypeError" in out
