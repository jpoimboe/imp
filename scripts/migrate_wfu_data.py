"""One-time migration: rewrite the legacy `wait_for_user` tool shape in historical
change data into the bare-turn-end shape (awaiting_user_input + user_message), so the
retained rendering/replay back-compat can be deleted.

Throwaway — run once per control root after the wait_for_user removal (imp change
zaba25). Usage: `python scripts/migrate_wfu_data.py [CONTROL_ROOT] [--apply]`
(defaults to the current working directory; dry-run unless --apply)."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

Event = dict[str, Any]


def _is_wfu_call(e: Event) -> bool:
    return (e.get("type") == "tool_call"
            and (e.get("payload") or {}).get("name") == "wait_for_user")


def _wfu_call_ids(events: list[Event]) -> set[str | None]:
    return {(e.get("payload") or {}).get("id") for e in events if _is_wfu_call(e)}


def find_unsafe_events(events: list[Event]) -> list[str]:
    """wfu call ids that must NOT be auto-migrated: a resolved call with an error
    result, or with any tool_call/tool_result event between it and its result. A
    dangling call (no result) is safe and is not returned."""
    unsafe: list[str] = []
    positions = {(e.get("payload") or {}).get("id"): i
                 for i, e in enumerate(events) if _is_wfu_call(e)}
    for cid, ci in positions.items():
        ri = None
        for j in range(ci + 1, len(events)):
            p = events[j].get("payload") or {}
            if events[j].get("type") == "tool_result" and p.get("id") == cid:
                ri = j
                break
        if ri is None:
            continue  # dangling -> safe (handled by migrate_event_lines)
        if (events[ri].get("payload") or {}).get("error"):
            unsafe.append(cid)
            continue
        if any(events[k].get("type") in ("tool_call", "tool_result")
               for k in range(ci + 1, ri)):
            unsafe.append(cid)
    return unsafe


def migrate_event_lines(events: list[Event]) -> tuple[list[Event], int]:
    """Map each wfu tool_call -> awaiting_user_input and its paired tool_result ->
    user_message; a dangling call -> awaiting_user_input only. Returns (new_events,
    n_gates). Caller must ensure find_unsafe_events(events) is empty."""
    result_ids = _wfu_call_ids(events)
    out: list[Event] = []
    gates = 0
    for e in events:
        p = e.get("payload") or {}
        if _is_wfu_call(e):
            ne = dict(e)
            ne["type"] = "awaiting_user_input"
            ne["payload"] = {"prompt": ""}
            out.append(ne)
            gates += 1
        elif e.get("type") == "tool_result" and p.get("id") in result_ids:
            ne = dict(e)
            ne["type"] = "user_message"
            ne["payload"] = {"role": "user", "content": p.get("content") or []}
            out.append(ne)
        else:
            out.append(e)
    return out, gates


def _read_jsonl(path: Path) -> list[Event]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def _write_jsonl(path: Path, rows: list[Event]) -> None:
    path.write_text("".join(json.dumps(r) + "\n" for r in rows))


def migrate_change_events_file(path: Path, *, apply: bool) -> int:
    new_events, gates = migrate_event_lines(_read_jsonl(path))
    if gates and apply:
        _write_jsonl(path, new_events)
    return gates
