from __future__ import annotations

import io
import json
from contextlib import redirect_stderr, redirect_stdout
from datetime import UTC, datetime
from pathlib import Path

import pytest

from imp.cli.main import _overview_lines, main
from imp.engine.quest import Quest
from tests.conftest import _seed_control

_INTENT = "Fix the login bug"
_FRAMEWORK = "You are the coder agent, follow the plan exactly"
_MODEL = "Investigating the login flow now"
_REPLY = "please retry the deploy step"
_NOW = datetime(2026, 7, 15, 12, 0, 0, tzinfo=UTC)

# opening_intent -> intent; step_context(system_prompt) -> split; the human=False
# first-after-step_context user_message -> engine_message/agent_prompt (the
# framework prompt that WAS mis-rendered as a human turn); llm_response -> the
# model turn; human=True user_message -> the real human reply.
_OLD = [
    {"id": "i0", "parent_id": None, "agent_id": None, "timestamp": "T0",
     "type": "opening_intent", "payload": {"text": _INTENT}},
    {"id": "c1", "parent_id": None, "agent_id": "A", "timestamp": "T1",
     "type": "step_context",
     "payload": {"provider": "anthropic", "model": "m", "system_prompt": "sys",
                 "tools_schema": [], "params": {}, "max_tool_calls": 1}},
    {"id": "p1", "parent_id": "c1", "agent_id": "A", "timestamp": "T2",
     "type": "user_message",
     "payload": {"role": "user", "content": [{"type": "text", "text": _FRAMEWORK}],
                 "human": False}},
    {"id": "r1", "parent_id": "p1", "agent_id": "A", "timestamp": "T3",
     "type": "llm_response",
     "payload": {"provider": "anthropic", "model": "m",
                 "raw": {"content": [{"type": "text", "text": _MODEL}]},
                 "usage": {"input_tokens": 1}, "stop_reason": "end_turn"}},
    {"id": "a1", "parent_id": "r1", "agent_id": "A", "timestamp": "T4",
     "type": "awaiting_user_input", "payload": {}},
    {"id": "u2", "parent_id": "a1", "agent_id": "A", "timestamp": "T5",
     "type": "user_message",
     "payload": {"role": "user", "content": [{"type": "text", "text": _REPLY}],
                 "human": True}},
]


def _seed_old_quest(control: Path, cid: str) -> None:
    ch = Quest.create(control, title="", quest_id=cid, base_branch="main")
    ch.events_path.write_text(
        "".join(json.dumps(e) + "\n" for e in _OLD), encoding="utf-8")
    meta_path = ch.quest_dir / "quest.json"
    meta = json.loads(meta_path.read_text())
    meta["layout_version"] = 5
    meta_path.write_text(json.dumps(meta, indent=2, sort_keys=True))


def _run_log(monkeypatch: pytest.MonkeyPatch, control: Path,
             *argv: str) -> tuple[int, str, str]:
    import imp.cli.main as m
    monkeypatch.setattr(m, "_control_root", lambda: control)
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        rc = main(["log", *argv])
    return rc, out.getvalue(), err.getvalue()


def test_imp_list_shows_real_intent_not_framework_prompt(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    _seed_old_quest(tmp_path, "old001")

    Quest.load(tmp_path, "old001")  # the sanctioned migration trigger

    out = "\n".join(
        _overview_lines(tmp_path, show_all=True, now=_NOW, color=False))
    assert _INTENT in out
    assert _FRAMEWORK not in out


def test_imp_log_shows_real_turns_not_framework_prompt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_control(tmp_path)
    _seed_old_quest(tmp_path, "old002")

    Quest.load(tmp_path, "old002")
    rc, out, err = _run_log(monkeypatch, tmp_path, "--full", "--no-pager", "old002")

    assert rc == 0, err
    assert f"> {_INTENT}" in out   # intent migrated → user_message → `>` turn
    assert f"• {_MODEL}" in out    # llm_response migrated → agent_message → `•` prose
    # The real typed human reply renders AS a `>` human-turn line (color off, so
    # the caret is the literal ">"; it marks the turn's first line, lstrip covers
    # any indent). Positive check per AC 4's "with the `>` caret".
    reply_lines = [ln for ln in out.splitlines() if _REPLY in ln]
    assert reply_lines and reply_lines[0].lstrip().startswith(">")
    # ...while the framework prompt is NOT a `>` human turn (the reported symptom).
    assert f"> {_FRAMEWORK}" not in out
