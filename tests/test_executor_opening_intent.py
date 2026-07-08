"""run_change emits a display-only opening_intent event once on a fresh run,
right after tiers_resolved; never on a resume, never when seed_opening is empty."""
from __future__ import annotations

from pathlib import Path

import pytest

from imp.engine.executor import RunResult, run_change
from imp.engine.state import JsonlEventLog, State
from tests.test_executor import (
    _make_change,
    _register_invocation_capturing,
    _register_prompt_capturing,
)

_FAKE_CONFIG = (
    "providers: [fake]\ntiers:\n  low: {fake: m}\n  medium: {fake: m}\n  high: {fake: m}\n"
)


@pytest.fixture
def anyio_backend():
    return "asyncio"


def _task_text(inv: object) -> str:
    """The rendered agent body (where `run.opening` interpolates) is the first
    user message for a top-level step, not the system prompt."""
    return inv.initial_messages[0].content[0].text


_WRITER_BODY = (
    "---\nname: writer\ntools: [complete_step]\n"
    "output:\n  status: {type: enum, values: [Success, Error]}\n---\n"
    # Blank line after the marker keeps it a standalone paragraph so reflow_markdown
    # (applied to the body) never wraps other text into the "INTENT: ..." line.
    "{% if run.opening %}INTENT: {{ run.opening }}\n\n{% endif %}go {{ change.id }}\n"
)


def _seed_render(root: Path) -> object:
    """One-agent-step workflow whose prompt echoes `run.opening` as `INTENT: ...`."""
    wf = root / "imp" / "workflows" / "imp"
    (wf / "agents").mkdir(parents=True)
    (wf / "agents" / "writer.md").write_text(_WRITER_BODY)
    (wf / "workflow.yaml").write_text(
        "name: t\ndescription: t\n"
        "defaults: {max_retries: 1, max_loops: 6}\n"
        "phases:\n  - name: only\n    steps:\n"
        "      - {name: w, file: agents/writer.md, tier: low}\n"
    )
    (root / "imp" / "project.yaml").write_text("default-workflow: imp\n")
    (root / ".imp").mkdir(parents=True, exist_ok=True)
    (root / ".imp" / "config.yaml").write_text(_FAKE_CONFIG)
    return _make_change(root)


def _seed_two_step(root: Path) -> object:
    """Two agent steps (a, b); both echo `run.opening`. Used to prove the seed
    reaches only the FIRST agent step, never a downstream one."""
    wf = root / "imp" / "workflows" / "imp"
    (wf / "agents").mkdir(parents=True)
    (wf / "agents" / "writer.md").write_text(_WRITER_BODY)
    (wf / "workflow.yaml").write_text(
        "name: t\ndescription: t\n"
        "defaults: {max_retries: 1, max_loops: 6}\n"
        "phases:\n  - name: only\n    steps:\n"
        "      - {name: a, file: agents/writer.md, tier: low}\n"
        "      - {name: b, file: agents/writer.md, tier: low}\n"
    )
    (root / "imp" / "project.yaml").write_text("default-workflow: imp\n")
    (root / ".imp").mkdir(parents=True, exist_ok=True)
    (root / ".imp" / "config.yaml").write_text(_FAKE_CONFIG)
    return _make_change(root)


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
        "      - {name: w, file: agents/writer.md, tier: low}\n"
    )
    (root / "imp" / "project.yaml").write_text("default-workflow: imp\n")
    (root / ".imp").mkdir(parents=True, exist_ok=True)
    (root / ".imp" / "config.yaml").write_text(_FAKE_CONFIG)
    return _make_change(root)


@pytest.mark.anyio
async def test_seeded_first_run_interpolates_opening_from_durable_field(
    tmp_path: Path,
) -> None:
    change = _seed_render(tmp_path)
    change.seed_opening = "add a --json flag"   # durable source; no run parameter
    sink: list = []
    _register_invocation_capturing({"status": "Success"}, sink)

    result = await run_change(change)
    assert result is RunResult.COMPLETED
    assert "INTENT: add a --json flag" in _task_text(sink[0])


@pytest.mark.anyio
async def test_restart_resume_reinterpolates_opening_from_durable_field(
    tmp_path: Path,
) -> None:
    # dobe93: the opening turn errored, leaving an empty transcript; the resume
    # re-renders the entry step FRESH and must re-seed from change.seed_opening.
    change = _seed_render(tmp_path)
    change.seed_opening = "add a --json flag"
    state = State.load(change.state_dir)
    state.step = "w"
    state.arm_step_resume(interrupt=False)      # resume_pending == step -> is_resume
    state.save(change.state_dir)                # no transcript written -> restart
    sink: list = []
    _register_invocation_capturing({"status": "Success"}, sink)

    result = await run_change(change)
    assert result is RunResult.COMPLETED
    assert "INTENT: add a --json flag" in _task_text(sink[0])


@pytest.mark.anyio
async def test_unseeded_run_renders_no_opening(tmp_path: Path) -> None:
    change = _seed_render(tmp_path)             # change.seed_opening stays None
    sink: list = []
    _register_invocation_capturing({"status": "Success"}, sink)

    result = await run_change(change)
    assert result is RunResult.COMPLETED
    assert "INTENT:" not in _task_text(sink[0])


@pytest.mark.anyio
async def test_opening_not_delivered_to_downstream_step(tmp_path: Path) -> None:
    # A resume landing on a LATER agent step must never receive the one-shot seed.
    change = _seed_two_step(tmp_path)
    change.seed_opening = "add a --json flag"
    state = State.load(change.state_dir)
    state.step = "b"                            # start at the downstream step
    state.arm_step_resume(interrupt=False)
    state.save(change.state_dir)
    sink: list = []
    _register_invocation_capturing({"status": "Success"}, sink)

    result = await run_change(change)
    assert result is RunResult.COMPLETED
    assert "INTENT:" not in _task_text(sink[0])  # step "b" got no seed


@pytest.mark.anyio
async def test_opening_intent_emitted_from_seed_opening(tmp_path: Path) -> None:
    change = _seed(tmp_path)
    change.seed_opening = "add a --json flag"
    _register_prompt_capturing({"status": "Success"}, [])
    result = await run_change(change)
    assert result is RunResult.COMPLETED

    types = [e["type"] for e in JsonlEventLog(change.events_path).read()]
    assert "opening_intent" in types
    assert types.index("opening_intent") == types.index("tiers_resolved") + 1
    payload = next(e["payload"] for e in JsonlEventLog(change.events_path).read()
                   if e["type"] == "opening_intent")
    assert payload == {"text": "add a --json flag"}


@pytest.mark.anyio
async def test_no_opening_intent_when_seed_opening_absent_or_empty(tmp_path: Path) -> None:
    change = _seed(tmp_path)                    # change.seed_opening is None
    _register_prompt_capturing({"status": "Success"}, [])
    await run_change(change)
    types = [e["type"] for e in JsonlEventLog(change.events_path).read()]
    assert "opening_intent" not in types

    (tmp_path / "other").mkdir()
    change2 = _seed(tmp_path / "other")
    change2.seed_opening = ""                   # empty string is suppressed (falsy)
    _register_prompt_capturing({"status": "Success"}, [])
    await run_change(change2)
    types2 = [e["type"] for e in JsonlEventLog(change2.events_path).read()]
    assert "opening_intent" not in types2
