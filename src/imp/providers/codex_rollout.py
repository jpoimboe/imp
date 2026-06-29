"""Persist and restore codex's native on-disk rollout across an imp pause.

codex is stateful via a rollout file at
`$CODEX_HOME/sessions/…/rollout-<ts>-<thread_id>.jsonl`. imp provisions a fresh
per-run CODEX_HOME and `shutil.rmtree`s it at teardown, so the rollout — the
actual conversation context — is deleted at the pause. To resume, imp must own
that file across the pause: copy it (and the thread_id) into the durable
`transcript_dir` on pause, restore it into the fresh CODEX_HOME on resume, then
call `Codex.resume_thread(thread_id)`. Mechanics verified by
`docs/superpowers/spikes/codex/spike_codex_rollout_restore.py`.
"""
from __future__ import annotations

import json
import shutil
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from imp.agents.tools.limits import KEEP_RECENT_TURNS
from imp.providers.history_compaction import (
    EVICTION_PLACEHOLDER,
    HistoryRecord,
    results_to_evict,
)

_THREAD_JSON = "codex_thread.json"
_ROLLOUT_SUBDIR = "codex_rollout"


def _newest_matching_rollout(codex_home: Path, thread_id: str) -> Path | None:
    """The rollout file for `thread_id`. codex names it
    `rollout-<ts>-<thread_id>.jsonl`; prefer the newest name-match (a chained
    resume may write a fresh rollout for the same thread), else the sole rollout.
    """
    sessions = codex_home / "sessions"
    if not sessions.exists():
        return None
    rollouts = list(sessions.rglob("*.jsonl"))
    matches = [p for p in rollouts if thread_id in p.name]
    if matches:
        return max(matches, key=lambda p: p.stat().st_mtime)
    if len(rollouts) == 1:
        return rollouts[0]
    return None


def persist_rollout(*, codex_home: Path, thread_id: str, transcript_dir: Path) -> bool:
    """Copy codex's rollout out of the ephemeral CODEX_HOME into `transcript_dir`
    and record `thread_id` + the rollout's relative path. Call BEFORE
    `teardown_codex_home`. Returns True iff a rollout was persisted (False when
    codex wrote none yet — e.g. a run that failed before its first turn)."""
    rollout = _newest_matching_rollout(codex_home, thread_id)
    if rollout is None:
        return False
    rel = rollout.relative_to(codex_home)  # sessions/…/rollout-….jsonl
    dest = transcript_dir / _ROLLOUT_SUBDIR / rel
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(rollout, dest)
    (transcript_dir / _THREAD_JSON).write_text(
        json.dumps({"thread_id": thread_id, "rollout_relpath": str(rel)}))
    return True


def restore_rollout(*, codex_home: Path, transcript_dir: Path) -> str:
    """Restore the persisted rollout into the fresh CODEX_HOME and return the
    saved thread_id. Call AFTER provisioning the fresh CODEX_HOME and BEFORE
    `Codex.resume_thread`. Fails loudly (`FileNotFoundError`) if the thread
    metadata or rollout bytes are missing — converting otherwise-silent amnesia
    (teardown ran too early, ephemeral CI, cross-host) into a real error."""
    meta_path = transcript_dir / _THREAD_JSON
    if not meta_path.exists():
        raise FileNotFoundError(
            f"codex resume: no {_THREAD_JSON} in {transcript_dir} — "
            "rollout was not persisted on pause")
    meta = json.loads(meta_path.read_text())
    thread_id: str = meta["thread_id"]
    rel: str = meta["rollout_relpath"]
    saved = transcript_dir / _ROLLOUT_SUBDIR / rel
    if not saved.exists():
        raise FileNotFoundError(
            f"codex resume: persisted rollout missing at {saved}")
    dest = codex_home / rel
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(saved, dest)
    if not dest.exists():  # the spec's explicit "assert it exists" guard
        raise FileNotFoundError(
            f"codex resume: rollout restore failed at {dest}")
    return thread_id


def _canonical_tool_name(name: str | None) -> str | None:
    """Return the bare tool name after the last ``__`` separator, or None."""
    if name is None:
        return None
    return name.rsplit("__", 1)[-1]


def _rollout_records(objs: Iterable[dict[str, Any]]) -> list[HistoryRecord]:
    """Decode rollout objects into the neutral HistoryRecord sequence.

    A turn index increments at the start of each function_call batch (a
    function_call whose preceding tool record was an output, or the first call),
    so consecutive calls in one model turn share a turn and each function_call_output
    inherits its batch's turn.
    """
    records: list[HistoryRecord] = []
    turn = -1
    prev_was_call = False
    for o in objs:
        if o.get("type") != "response_item":
            continue
        p = o.get("payload")
        if not isinstance(p, dict):
            continue
        pt = p.get("type")
        if pt == "function_call":
            if not prev_was_call:
                turn += 1
            prev_was_call = True
            records.append(HistoryRecord(
                turn=turn, call_id=p.get("call_id"),
                call_name=_canonical_tool_name(p.get("name"))))
        elif pt == "function_call_output":
            prev_was_call = False
            records.append(HistoryRecord(
                turn=turn, result_id=p.get("call_id"),
                result_body=p.get("output")))
    return records


def _is_evicted_output(obj: dict[str, Any], evicted: frozenset[str]) -> bool:
    if obj.get("type") != "response_item":
        return False
    p = obj.get("payload")
    return (
        isinstance(p, dict)
        and p.get("type") == "function_call_output"
        and p.get("call_id") in evicted
    )


def evict_rollout_lines(
    lines: list[str], *, keep_recent_turns: int = KEEP_RECENT_TURNS,
) -> list[str]:
    """Clear the body of every old compactable function_call_output in a cold
    rollout, swapping payload.output for EVICTION_PLACEHOLDER. Non-matched lines
    are returned verbatim; the input list object is returned unchanged when
    nothing is evicted (byte-stable no-op). Callers gate on the cold signal —
    this is the cold-path rewrite, so the core is asked with cold=True.
    """
    parsed: list[tuple[str, dict[str, Any] | None]] = []
    for raw in lines:
        try:
            parsed.append((raw, json.loads(raw)))
        except json.JSONDecodeError:
            parsed.append((raw, None))
    records = _rollout_records(o for _, o in parsed if isinstance(o, dict))
    evicted = results_to_evict(
        records, cold=True, keep_recent_turns=keep_recent_turns)
    if not evicted:
        return lines
    out: list[str] = []
    for raw, obj in parsed:
        if isinstance(obj, dict) and _is_evicted_output(obj, evicted):
            obj["payload"]["output"] = EVICTION_PLACEHOLDER
            out.append(json.dumps(obj))
        else:
            out.append(raw)
    return out
