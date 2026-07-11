from __future__ import annotations

import re
from pathlib import Path

import pytest

from imp.agents.prompts import build_context, render_prompt
from imp.agents.reflow import reflow_markdown
from imp.engine.state import JsonlEventLog

_ASSET = Path("src/imp/assets/workflows/imp")

_FENCE = re.compile(r"^\s*(`{3,}|~{3,})")
_ATX = re.compile(r"^\s*#{1,6}\s")
_MARKER = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s")


def _fence_interiors(text: str) -> list[str]:
    interiors: list[str] = []
    inside = False
    fence_str = ""
    for line in text.split("\n"):
        if not inside:
            opened = _FENCE.match(line)
            if opened:
                inside = True
                fence_str = opened.group(1)
            continue
        char = re.escape(fence_str[0])
        if re.match(rf"^\s*{char}{{{len(fence_str)},}}\s*$", line):
            inside = False
            continue
        interiors.append(line)
    return interiors


def _count(pat: re.Pattern[str], text: str) -> int:
    return sum(1 for ln in text.split("\n") if pat.match(ln))


def _table_rows(text: str) -> int:
    return sum(1 for ln in text.split("\n") if ln.lstrip().startswith("|"))


def _md_assets() -> list[Path]:
    return sorted(_ASSET.glob("**/*.md"))


@pytest.mark.parametrize("path", _md_assets(), ids=lambda p: p.name)
def test_reflow_preserves_structure_in_asset(path: Path) -> None:
    src = path.read_text()
    out = reflow_markdown(src)
    # No line inside any fence is joined or otherwise altered.
    assert _fence_interiors(out) == _fence_interiors(src)
    # Structural-block line counts are conserved across the reflow.
    assert _count(_FENCE, out) == _count(_FENCE, src)
    assert _count(_ATX, out) == _count(_ATX, src)
    assert _count(_MARKER, out) == _count(_MARKER, src)
    assert _table_rows(out) == _table_rows(src)


@pytest.mark.parametrize("path", _md_assets(), ids=lambda p: p.name)
def test_reflow_idempotent_on_asset(path: Path) -> None:
    once = reflow_markdown(path.read_text())
    assert reflow_markdown(once) == once


def test_reflow_folds_wrapped_lines_in_rendered_persona(tmp_path: Path) -> None:
    log = JsonlEventLog(tmp_path / "events.jsonl")
    ctx = build_context(quest_title="t", event_log=log)
    rendered = render_prompt((_ASSET / "persona.md").read_text(), ctx,
                             workflow_dir=_ASSET)
    out = reflow_markdown(rendered)
    # Structure survives the rendered persona body.
    assert _fence_interiors(out) == _fence_interiors(rendered)
    assert _count(_FENCE, out) == _count(_FENCE, rendered)
    assert _count(_MARKER, out) == _count(_MARKER, rendered)
    assert _count(_ATX, out) == _count(_ATX, rendered)
    assert _table_rows(out) == _table_rows(rendered)
    # And reflow actually folded soft-wrapped lines (guards against a no-op).
    assert len(out.split("\n")) < len(rendered.split("\n"))
