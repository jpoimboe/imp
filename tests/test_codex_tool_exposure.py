"""Static fast-suite guards on the imp tool names codex can see and the names
imp's codex-facing prompts tell it to call.

The live probe prompt runs only under `--run-live`, so a stale
`mcp__imp__`-prefixed instruction in it would not surface until the next live
run — this file catches it with no codex process involved.
"""
from __future__ import annotations

from pathlib import Path


def test_live_codex_prompts_name_imp_tools_by_their_bare_names() -> None:
    src = (Path(__file__).resolve().parent / "test_codex_provider_live.py").read_text()
    assert "mcp__imp__" not in src
