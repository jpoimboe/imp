from __future__ import annotations

import argparse
import io
import json
from collections.abc import AsyncIterator
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from typing import Any

import pytest

from imp.agents.runner import AgentInvocation, AgentRunner, Event
from imp.cli.main import _select_log_events, main
from imp.engine.executor import RunResult, run_quest
from imp.engine.quest import LAYOUT_VERSION
from imp.engine.resume_state import NativeResumeState
from imp.engine.state import JsonlEventLog, State
from imp.paths import project_state_dir
from tests.test_executor_resume_rerun import (
    _seed_native_manifest,
    _seed_single_agent,
)

# Two steps (spec@1-3, plan@4-6); agent_message authors carry step:<phase>/<name>
# agent_ids so agent_display renders "spec" / "plan".
_EVENTS = [
    {"id": "e1", "type": "step_started", "agent_id": None,
     "payload": {"step": "spec"}},
    {"id": "e2", "type": "agent_message", "agent_id": "step:design/spec",
     "payload": {"content": [{"type": "text", "text": "alpha"}]}},
    {"id": "e3", "type": "step_completed", "agent_id": None,
     "payload": {"step": "spec"}},
    {"id": "e4", "type": "step_started", "agent_id": None,
     "payload": {"step": "plan"}},
    {"id": "e5", "type": "agent_message", "agent_id": "step:build/plan",
     "payload": {"content": [{"type": "text", "text": "beta"}]}},
    {"id": "e6", "type": "step_completed", "agent_id": None,
     "payload": {"step": "plan"}},
]


def _ns(**kw: object) -> argparse.Namespace:
    base = {"step": None, "range": None, "range_pos": None,
            "agent": None, "limit": None}
    base.update(kw)
    return argparse.Namespace(**base)


def test_select_log_events_no_filters_is_identity() -> None:
    assert _select_log_events(_ns(), "q", _EVENTS) == _EVENTS


def test_select_log_events_step_scopes_to_window() -> None:
    out = _select_log_events(_ns(step=["spec"]), "q", _EVENTS)
    assert [e["id"] for e in out] == ["e1", "e2", "e3"]


def test_select_log_events_agent_keeps_matching_author() -> None:
    out = _select_log_events(_ns(agent=["plan"]), "q", _EVENTS)
    assert [e["id"] for e in out] == ["e5"]


def test_select_log_events_positional_range_scopes() -> None:
    out = _select_log_events(_ns(range_pos="1-2"), "q", _EVENTS)
    assert [e["id"] for e in out] == ["e1", "e2"]


def test_select_log_events_range_option_scopes() -> None:
    out = _select_log_events(_ns(range=["4-6"]), "q", _EVENTS)
    assert [e["id"] for e in out] == ["e4", "e5", "e6"]


def test_select_log_events_limit_keeps_last_n() -> None:
    out = _select_log_events(_ns(limit=2), "q", _EVENTS)
    assert [e["id"] for e in out] == ["e5", "e6"]


def test_select_log_events_unknown_step_returns_2(
    capsys: pytest.CaptureFixture[str],
) -> None:
    rc = _select_log_events(_ns(step=["ghost"]), "abc", _EVENTS)
    assert rc == 2
    assert capsys.readouterr().err == "imp: no step 'ghost' in abc; steps: spec, plan\n"


def test_select_log_events_bad_range_returns_2(
    capsys: pytest.CaptureFixture[str],
) -> None:
    rc = _select_log_events(_ns(range_pos="notanumber"), "abc", _EVENTS)
    assert rc == 2
    assert "invalid range" in capsys.readouterr().err


# Same two-step shape as _EVENTS, written through the real event log so
# ordinals/timestamps are assigned exactly as production does.
_LOG_EVENTS = [
    {"type": "step_started", "agent_id": None, "payload": {"step": "spec"}},
    {"type": "agent_message", "agent_id": "step:design/spec",
     "payload": {"provider": "anthropic", "model": "m",
                 "raw": {"content": [{"type": "text", "text": "alpha"}]},
                 "usage": {"input_tokens": 1, "output_tokens": 1},
                 "stop_reason": "end_turn"}},
    {"type": "step_completed", "agent_id": None, "payload": {"step": "spec"}},
    {"type": "step_started", "agent_id": None, "payload": {"step": "plan"}},
    {"type": "agent_message", "agent_id": "step:build/plan",
     "payload": {"provider": "anthropic", "model": "m",
                 "raw": {"content": [{"type": "text", "text": "beta"}]},
                 "usage": {"input_tokens": 1, "output_tokens": 1},
                 "stop_reason": "end_turn"}},
    {"type": "step_completed", "agent_id": None, "payload": {"step": "plan"}},
]

# Two `warning` events (one per step window) whose messages carry unique markers.
# A warning renders as a plain "warning: <message>" muted line in the default lean
# view (no provider parse chain), so it is a reliable end-to-end render probe.
_MARKED_EVENTS = [
    {"type": "step_started", "agent_id": None, "payload": {"step": "spec"}},
    {"type": "warning", "agent_id": None,
     "payload": {"source": "s", "message": "SPECMARK skipped", "detail": {}}},
    {"type": "step_completed", "agent_id": None, "payload": {"step": "spec"}},
    {"type": "step_started", "agent_id": None, "payload": {"step": "plan"}},
    {"type": "warning", "agent_id": None,
     "payload": {"source": "s", "message": "PLANMARK skipped", "detail": {}}},
    {"type": "step_completed", "agent_id": None, "payload": {"step": "plan"}},
]


def _make_live_quest(control: Path, cid: str, events: list[dict]) -> None:
    d = project_state_dir(control) / "quests" / cid
    d.mkdir(parents=True, exist_ok=True)
    (d / "quest.json").write_text(json.dumps({
        "id": cid, "title": cid.title(), "base_branch": "main",
        "project_root": str(control), "layout_version": LAYOUT_VERSION}))
    log = JsonlEventLog(d / "events.jsonl")
    for e in events:
        log.append(agent_id=e.get("agent_id"), type=e["type"],
                   payload=e.get("payload", {}))


def _capture_build_tree(monkeypatch) -> dict:
    """Patch narrative.build_tree to record each event list it receives, in call order."""
    import imp.cli.narrative as nv
    real = nv.build_tree
    captured: dict = {"calls": []}

    def fake(events):
        captured["calls"].append(list(events))
        return real([])

    monkeypatch.setattr(nv, "build_tree", fake)
    return captured


def _run_log(monkeypatch, control: Path, *argv: str) -> tuple[int, str, str]:
    import imp.cli.main as m
    monkeypatch.setattr(m, "_control_root", lambda: control)
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        rc = main(["log", *argv])
    return rc, out.getvalue(), err.getvalue()


def test_log_no_filters_passes_all_events(tmp_path, monkeypatch) -> None:
    _make_live_quest(tmp_path, "logf01", _LOG_EVENTS)
    cap = _capture_build_tree(monkeypatch)
    rc, _out, _err = _run_log(monkeypatch, tmp_path, "logf01")
    assert rc == 0
    assert len(cap["calls"][0]) == 6


def test_log_unfiltered_builds_tree_once(tmp_path, monkeypatch) -> None:
    _make_live_quest(tmp_path, "logf01", _LOG_EVENTS)
    cap = _capture_build_tree(monkeypatch)
    rc, _out, _err = _run_log(monkeypatch, tmp_path, "logf01")
    assert rc == 0
    assert len(cap["calls"]) == 1


def test_log_filtered_also_builds_full_log(tmp_path, monkeypatch) -> None:
    _make_live_quest(tmp_path, "logf01", _LOG_EVENTS)
    cap = _capture_build_tree(monkeypatch)
    rc, _out, _err = _run_log(monkeypatch, tmp_path, "logf01", "1-2")
    assert rc == 0
    assert [len(events) for events in cap["calls"]] == [2, 6]


def test_log_step_filter_scopes_narrative(tmp_path, monkeypatch) -> None:
    _make_live_quest(tmp_path, "logf01", _LOG_EVENTS)
    cap = _capture_build_tree(monkeypatch)
    rc, _out, _err = _run_log(monkeypatch, tmp_path, "logf01", "--step", "spec")
    assert rc == 0
    assert [e["type"] for e in cap["calls"][0]] == [
        "step_started", "agent_message", "step_completed"]
    assert all(e.get("agent_id") != "step:build/plan" for e in cap["calls"][0])


def test_log_positional_range_scopes_narrative(tmp_path, monkeypatch) -> None:
    _make_live_quest(tmp_path, "logf01", _LOG_EVENTS)
    cap = _capture_build_tree(monkeypatch)
    rc, _out, _err = _run_log(monkeypatch, tmp_path, "logf01", "1-2")
    assert rc == 0
    assert [e["type"] for e in cap["calls"][0]] == ["step_started", "agent_message"]


def test_log_limit_keeps_last_n(tmp_path, monkeypatch) -> None:
    _make_live_quest(tmp_path, "logf01", _LOG_EVENTS)
    cap = _capture_build_tree(monkeypatch)
    rc, _out, _err = _run_log(monkeypatch, tmp_path, "logf01", "--limit", "2")
    assert rc == 0
    assert [e["type"] for e in cap["calls"][0]] == [
        "agent_message", "step_completed"]


def test_log_agent_filter_keeps_matching_author(tmp_path, monkeypatch) -> None:
    _make_live_quest(tmp_path, "logf01", _LOG_EVENTS)
    cap = _capture_build_tree(monkeypatch)
    rc, _out, _err = _run_log(monkeypatch, tmp_path, "logf01", "--agent", "plan")
    assert rc == 0
    assert [e.get("agent_id") for e in cap["calls"][0]] == ["step:build/plan"]


def test_log_unknown_step_errors(tmp_path, monkeypatch) -> None:
    _make_live_quest(tmp_path, "logf01", _LOG_EVENTS)
    rc, _out, err = _run_log(monkeypatch, tmp_path, "logf01", "--step", "ghost")
    assert rc == 2
    assert "no step 'ghost'" in err


def test_log_md_export_honors_filters(tmp_path, monkeypatch) -> None:
    _make_live_quest(tmp_path, "logf01", _LOG_EVENTS)
    cap = _capture_build_tree(monkeypatch)
    rc, _out, _err = _run_log(
        monkeypatch, tmp_path, "logf01", "--step", "spec", "--md", "-")
    assert rc == 0
    assert [e["type"] for e in cap["calls"][0]] == [
        "step_started", "agent_message", "step_completed"]


def test_log_step_filter_renders_only_scoped_content(tmp_path, monkeypatch) -> None:
    # End-to-end: no build_tree mock — let render_tree produce the real narrative
    # and assert the kept step's content is present and the dropped step's absent.
    _make_live_quest(tmp_path, "logf02", _MARKED_EVENTS)
    rc, out, _err = _run_log(monkeypatch, tmp_path, "logf02", "--step", "spec")
    assert rc == 0
    assert "SPECMARK" in out          # inside the spec window: rendered
    assert "PLANMARK" not in out      # plan window filtered out: never rendered
    # Baseline: unfiltered, both markers render.
    rc2, out2, _err2 = _run_log(monkeypatch, tmp_path, "logf02")
    assert rc2 == 0
    assert "SPECMARK" in out2 and "PLANMARK" in out2


def test_log_html_export_honors_filters(tmp_path, monkeypatch) -> None:
    _make_live_quest(tmp_path, "logf01", _LOG_EVENTS)
    cap = _capture_build_tree(monkeypatch)
    rc, _out, _err = _run_log(
        monkeypatch, tmp_path, "logf01", "--step", "spec", "--html", "-")
    assert rc == 0
    assert [e["type"] for e in cap["calls"][0]] == [
        "step_started", "agent_message", "step_completed"]


@pytest.mark.anyio
async def test_log_preserves_narrative_across_native_only_resume(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, provider_registry,
) -> None:
    quest = _seed_single_agent(tmp_path, provider="codex")
    state = State.load(quest.state_dir)
    state.resume_pending = state.step
    state.save(quest.state_dir)
    session_dir = quest.quest_dir / "steps" / "only" / "w"
    opaque = _seed_native_manifest(
        session_dir, provider="codex", state={"thread": {"id": "log-native"}})
    log = quest.event_log()
    log.append(
        agent_id=None, type="user_message",
        payload={"role": "user", "subtype": "intent",
                 "content": [{"type": "text", "text": "ORIGINAL REQUEST"}]})
    log.append(
        agent_id=None, type="step_started",
        payload={"step": "w", "kind": "agent", "phase": "only",
                 "provider": "codex", "model": "m", "tools": [],
                 "resumed": False, "restarted": False})
    log.append(
        agent_id="step:only/w", type="engine_message",
        payload={"role": "system", "subtype": "system_prompt",
                 "content": [{"type": "text", "text": "stored"}]})
    log.append(
        agent_id="step:only/w", type="agent_message",
        payload={"provider": "anthropic", "model": "m",
                 "raw": {"content": [
                     {"type": "text", "text": "BEFORE INTERRUPTION"}]},
                 "usage": {"input_tokens": 1, "output_tokens": 1},
                 "stop_reason": "end_turn"})
    log.append(
        agent_id=None, type="workflow_paused",
        payload={"reason": "interrupt", "interrupt_reason": "ctrl-c"})
    log.append(agent_id=None, type="workflow_resumed", payload={})
    seen: list[AgentInvocation] = []

    class _Runner:
        async def run(self, inv: AgentInvocation) -> AsyncIterator[Event]:
            seen.append(inv)
            yield {"type": "agent_message", "payload": {
                "provider": "anthropic", "model": "m",
                "raw": {"content": [
                    {"type": "text", "text": "AFTER INTERRUPTION"}]},
                "usage": {"input_tokens": 1, "output_tokens": 1},
                "stop_reason": "end_turn"}}
            yield {"type": "agent_result",
                   "payload": {"outcome": "ok", "status": "Success"}}

        async def interrupt(self) -> None:
            pass

    class _P:
        name = "codex"
        model = "m"

        def create_runner(self, _i: AgentInvocation) -> AgentRunner:
            return _Runner()

    provider_registry("codex", env_check=lambda: True, factory=lambda _m: _P())
    events: list[dict[str, Any]] = []

    assert await run_quest(quest, on_event=events.append) is RunResult.COMPLETED
    assert seen[0].native_resume_state == NativeResumeState(
        provider="codex", state=opaque)
    resumed = [e for e in events
               if e["type"] == "step_started" and e["payload"].get("resumed")]
    assert len(resumed) == 1
    assert resumed[0]["payload"]["restarted"] is False

    rc, out, err = _run_log(monkeypatch, tmp_path, quest.id)
    assert (rc, err) == (0, "")
    assert (out.index("ORIGINAL REQUEST")
            < out.index("BEFORE INTERRUPTION")
            < out.index("AFTER INTERRUPTION"))


def test_log_no_type_flag(tmp_path, monkeypatch) -> None:
    import imp.cli.main as m
    _make_live_quest(tmp_path, "logf01", _LOG_EVENTS)
    monkeypatch.setattr(m, "_control_root", lambda: tmp_path)
    err = io.StringIO()
    with redirect_stderr(err), pytest.raises(SystemExit) as exc:
        main(["log", "logf01", "--type", "agent_message"])
    assert exc.value.code == 2
    assert "unrecognized arguments: --type" in err.getvalue()
