# tests/test_ask_user_tool.py
from __future__ import annotations

from pathlib import Path

import pytest

from imp.agents.tools import get_registry
from imp.agents.tools.context import ToolContext
import imp.agents.tools.ask_user  # noqa: F401


def _ctx(tmp_path: Path, input_fn=None) -> ToolContext:
    artifacts = tmp_path / "_artifacts"
    artifacts.mkdir()
    ctx = ToolContext(
        project_root=tmp_path, artifacts_dir=artifacts,
        fs_permissions=(), events_path=tmp_path / "events.jsonl",
    )
    ctx.user_input_fn = input_fn
    return ctx


def test_ask_user_returns_user_input(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path, input_fn=lambda prompt: "the answer")
    out = get_registry()["ask_user"].invoke(ctx, {"question": "what?"})
    assert out == "the answer"


def test_ask_user_without_handler_raises(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path, input_fn=None)
    with pytest.raises(RuntimeError, match="not available"):
        get_registry()["ask_user"].invoke(ctx, {"question": "x"})
