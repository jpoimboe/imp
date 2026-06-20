"""run_change emits a display-only opening_intent event once on a fresh run,
right after tiers_resolved; never on a resume, never when the param is empty."""
from __future__ import annotations

from pathlib import Path

import pytest

from imp.engine.executor import RunResult, run_change
from imp.engine.state import JsonlEventLog
from tests.test_executor import _make_change, _register_prompt_capturing

_FAKE_CONFIG = (
    "providers: [fake]\ntiers:\n  low: {fake: m}\n  medium: {fake: m}\n  high: {fake: m}\n"
)


@pytest.fixture
def anyio_backend():
    return "asyncio"


def _seed(root: Path) -> object:
    wf = root / "imp" / "workflows" / "imp"
    (wf / "agents").mkdir(parents=True)
    (wf / "agents" / "writer.md").write_text(
        "---\nname: writer\ntools: [complete_step]\n"
        "output:\n  status: {type: enum, values: [Success, Error]}\n---\n"
        "go {{ change.id }}\n"
    )
    (wf / "workflow.yaml").write_text(
        "name: t\ndescription: t\n"
        "defaults: {max_retries: 1, max_loops: 6}\n"
        "phases:\n  - name: only\n    steps:\n"
        "      - {name: w, file: agents/writer.md, tier: medium}\n"
    )
    (root / "imp" / "project.yaml").write_text("default-workflow: imp\n")
    (root / ".imp").mkdir(parents=True, exist_ok=True)
    (root / ".imp" / "config.yaml").write_text(_FAKE_CONFIG)
    return _make_change(root)


@pytest.mark.anyio
async def test_opening_intent_emitted_after_tiers_resolved(tmp_path: Path) -> None:
    change = _seed(tmp_path)
    _register_prompt_capturing({"status": "Success"}, [])
    result = await run_change(change, opening_intent="add a --json flag")
    assert result is RunResult.COMPLETED

    types = [e["type"] for e in JsonlEventLog(change.events_path).read()]
    assert "opening_intent" in types
    assert types.index("opening_intent") == types.index("tiers_resolved") + 1
    payload = next(e["payload"] for e in JsonlEventLog(change.events_path).read()
                   if e["type"] == "opening_intent")
    assert payload == {"text": "add a --json flag"}


@pytest.mark.anyio
async def test_no_opening_intent_when_param_absent_or_empty(tmp_path: Path) -> None:
    change = _seed(tmp_path)
    _register_prompt_capturing({"status": "Success"}, [])
    await run_change(change)  # default None
    types = [e["type"] for e in JsonlEventLog(change.events_path).read()]
    assert "opening_intent" not in types

    (tmp_path / "other").mkdir()
    change2 = _seed(tmp_path / "other")
    _register_prompt_capturing({"status": "Success"}, [])
    await run_change(change2, opening_intent="")  # empty string is suppressed
    types2 = [e["type"] for e in JsonlEventLog(change2.events_path).read()]
    assert "opening_intent" not in types2
