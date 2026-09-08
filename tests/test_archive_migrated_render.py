from __future__ import annotations

import io
import json
import os
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


def _seed_old_archived_quest(control: Path, cid: str,
                             events: list[dict] = _OLD) -> None:
    store = LocalBlobStore(archive.blobs_root(control))
    events_hash = store.put(
        "".join(json.dumps(e) + "\n" for e in events).encode("utf-8"))
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
    # After migration: opening_intent → user_message(subtype=intent) → > caret turn.
    intent_lines = [ln for ln in out_s.splitlines() if _INTENT in ln]
    assert intent_lines and intent_lines[0].lstrip().startswith(">")
    assert "opening_intent" not in out_s
    assert _MODEL in out_s           # model turn (former llm_response) rendered as agent prose
    assert "llm_response" not in out_s
    # The real typed human reply renders as a `>` human-turn line (color off under
    # redirect, so the caret is the literal ">"; it marks the turn's first line).
    reply_lines = [ln for ln in out_s.splitlines() if _REPLY in ln]
    assert reply_lines and reply_lines[0].lstrip().startswith(">")
    # ...while the framework prompt is NOT a `>` human turn (the reported symptom).
    assert f"> {_FRAMEWORK}" not in out_s


# Two entries of one looping step, exactly as the engine wrote them before this
# quest: no `round` key on either payload.
_PRE_CHANGE_ROUNDS = [
    {"id": "s1", "parent_id": None, "agent_id": None,
     "timestamp": "2026-06-10T00:00:00+00:00", "type": "step_started",
     "payload": {"step": "plan-reviewer", "kind": "agent", "phase": "planning",
                 "label": "reviewing plan"}},
    {"id": "c1", "parent_id": None, "agent_id": None,
     "timestamp": "2026-06-10T00:00:05+00:00", "type": "step_completed",
     "payload": {"step": "plan-reviewer", "kind": "agent",
                 "action": {"verb": "continue"}}},
    {"id": "s2", "parent_id": None, "agent_id": None,
     "timestamp": "2026-06-10T00:00:05+00:00", "type": "step_started",
     "payload": {"step": "plan-reviewer", "kind": "agent", "phase": "planning",
                 "label": "reviewing plan"}},
    {"id": "c2", "parent_id": None, "agent_id": None,
     "timestamp": "2026-06-10T00:00:10+00:00", "type": "step_completed",
     "payload": {"step": "plan-reviewer", "kind": "agent",
                 "action": {"verb": "continue"}}},
]

_UNMARKED = ["reviewing plan",
             "reviewing plan · complete · 5s",
             "reviewing plan",
             "reviewing plan · complete · 5s"]


def _log_lines(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cid: str,
               needle: str) -> list[str]:
    import imp.cli.main as m
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(m, "_control_root", lambda: tmp_path)
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        rc = main(["log", "--no-pager", cid])
    assert rc == 0, err.getvalue()
    return [ln.strip() for ln in out.getvalue().splitlines() if needle in ln]


def test_a_migrated_pre_change_quest_renders_no_round_and_no_tally(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The backfill stamps round 1, which renders no segment — so the quest reads
    # exactly as it did before this change, repeat entries included.
    _seed_control(tmp_path)
    _seed_old_archived_quest(tmp_path, "rnd10", _PRE_CHANGE_ROUNDS)

    archive.ensure_current(tmp_path)

    assert archive.data_version_path(tmp_path).read_text().strip() == str(
        archive._DATA_VERSION)                       # the chain ran over the blob
    assert _log_lines(tmp_path, monkeypatch, "rnd10", "reviewing plan") == _UNMARKED


def test_an_unmigrated_blob_on_a_read_only_archive_renders_the_same(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A read-only archive root makes ensure_current a no-op, so these payloads
    # reach the renderer un-stamped; the seam's default is what carries them.
    _seed_control(tmp_path)
    _seed_old_archived_quest(tmp_path, "rnd11", _PRE_CHANGE_ROUNDS)
    # The runner may be root, where mode bits do not deny a write — deny the probe.
    real_access = os.access
    root = archive.archive_root(tmp_path)
    monkeypatch.setattr(
        archive.os, "access",
        lambda p, mode: False if (mode == os.W_OK and str(p) == str(root))
        else real_access(p, mode))

    archive.ensure_current(tmp_path)

    assert not archive.data_version_path(tmp_path).exists()   # migration never ran
    assert _log_lines(tmp_path, monkeypatch, "rnd11", "reviewing plan") == _UNMARKED
