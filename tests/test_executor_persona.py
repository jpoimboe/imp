from __future__ import annotations

from pathlib import Path

import pytest

from imp.agents.reflow import reflow_markdown
from imp.engine.agent_run import _CORE_HEAD, _CORE_TAIL
from imp.engine.executor import RunResult, run_change
from tests.test_executor import _make_change, _register_invocation_capturing


@pytest.fixture
def anyio_backend():
    return "asyncio"


_FAKE_CONFIG = (
    "providers: [fake]\ntiers:\n  low: {fake: m}\n  medium: {fake: m}\n  high: {fake: m}\n"
)


def _seed(root: Path, *, persona: str | None) -> object:
    wf = root / "imp" / "workflows" / "imp"
    (wf / "agents").mkdir(parents=True)
    (wf / "agents" / "writer.md").write_text(
        "---\nname: writer\ntools: [complete_step]\n"
        "output:\n  status: {type: enum, values: [Success, Error]}\n---\n"
        "STEP BODY MARKER\n"
    )
    (wf / "workflow.yaml").write_text(
        "name: t\ndescription: t\n"
        "defaults: {max_retries: 1, max_loops: 6}\n"
        "phases:\n  - name: only\n    steps:\n"
        "      - {name: w, file: agents/writer.md, tier: low}\n"
    )
    if persona is not None:
        (wf / "persona.md").write_text(persona)
    (root / "imp" / "project.yaml").write_text("default-workflow: imp\n")
    (root / ".imp").mkdir(parents=True, exist_ok=True)
    (root / ".imp" / "config.yaml").write_text(_FAKE_CONFIG)
    return _make_change(root, title="My Quest")


@pytest.mark.anyio
async def test_executor_injects_rendered_persona_into_assembled_prompt(tmp_path: Path) -> None:
    change = _seed(tmp_path, persona="# Persona\nfor {{ change.title }}\n")
    sink: list = []
    _register_invocation_capturing({"status": "Success"}, sink)

    result = await run_change(change, on_event=lambda _e: None)
    assert result is RunResult.COMPLETED

    inv = sink[0]
    sp = inv.system_prompt
    body = inv.initial_messages[0].content[0].text
    # Persona is rendered (namespace substituted) and injected into the role-only
    # system prompt, after the core head; the step body moved to the user turn.
    assert "# Persona\nfor My Quest" in sp
    assert sp.index(reflow_markdown(_CORE_HEAD)) < sp.index("# Persona\nfor My Quest")
    assert "STEP BODY MARKER" in body


@pytest.mark.anyio
async def test_executor_personaless_workflow_assembles_without_persona(tmp_path: Path) -> None:
    change = _seed(tmp_path, persona=None)
    sink: list = []
    _register_invocation_capturing({"status": "Success"}, sink)

    result = await run_change(change, on_event=lambda _e: None)
    assert result is RunResult.COMPLETED

    inv = sink[0]
    sp = inv.system_prompt
    body = inv.initial_messages[0].content[0].text
    assert sp.startswith(reflow_markdown(f"{_CORE_HEAD}\n\n{_CORE_TAIL}"))
    assert "# Doing tasks" not in sp
    assert "STEP BODY MARKER" in body
