from __future__ import annotations

import io
import json
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

import pytest

from imp.cli.main import main
from imp.engine import archive
from imp.engine.archive import record
from imp.engine.archive.blobstore import LocalBlobStore
from tests.conftest import _seed_control

_INTENT = "Fix the login bug"
_FRAMEWORK = "You are the coder agent, follow the plan exactly"
_MODEL = "Investigating the login flow now"
_REPLY = "please retry the deploy step"

_OLD = [
    {"id": "i0", "parent_id": None, "agent_id": None, "timestamp": "T0",
     "type": "opening_intent", "payload": {"text": _INTENT}},
    {"id": "c1", "parent_id": None, "agent_id": "A", "timestamp": "T1",
     "type": "step_context",
     "payload": {"provider": "p", "model": "m", "system_prompt": "sys",
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


def _seed_old_archived_quest(control: Path, cid: str) -> None:
    store = LocalBlobStore(archive.blobs_root(control))
    events_hash = store.put(
        "".join(json.dumps(e) + "\n" for e in _OLD).encode("utf-8"))
    rec = record.build_record(
        {"id": cid, "title": "", "base_branch": "main",
         "created_at": "2026-06-01T00:00:00+00:00", "depends_on": []},
        {"id": cid, "status": "completed", "step": "finalize"},
        events_hash)
    record.write_record(archive.records_root(control), rec)
    archive.rebuild_index(control)


def test_imp_log_archived_migrated_quest_renders(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_control(tmp_path)
    _seed_old_archived_quest(tmp_path, "arc001")

    archive.ensure_current(tmp_path)  # the sanctioned archive trigger

    import imp.cli.main as m
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(m, "_control_root", lambda: tmp_path)
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        rc = main(["log", "--full", "--no-pager", "arc001"])
    out_s = out.getvalue()

    assert rc == 0, err.getvalue()
    assert _INTENT in out_s          # real opening intent rendered
    assert _MODEL in out_s           # model turn (former llm_response) rendered
    # The real typed human reply renders as a `>` human-turn line (color off under
    # redirect, so the caret is the literal ">"; it marks the turn's first line).
    reply_lines = [ln for ln in out_s.splitlines() if _REPLY in ln]
    assert reply_lines and reply_lines[0].lstrip().startswith(">")
    # ...while the framework prompt is NOT a `>` human turn (the reported symptom).
    assert f"> {_FRAMEWORK}" not in out_s
