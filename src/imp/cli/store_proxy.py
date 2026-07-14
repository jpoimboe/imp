"""Wire codec + socket transport for the read-only introspection Store.

`run_query` is the daemon-side dispatcher (one op -> one LocalStore call ->
JSON-safe value). `ProxyStore` is the in-sandbox transport that ships the same
ops to the daemon over the session socket. Bytes-valued fields are base64
strings on the wire; everything else is JSON-native."""
from __future__ import annotations

import base64
from typing import Any

from imp.cli.store import (
    CompletedRow,
    HomeDigest,
    QuestCard,
    Readiness,
    SearchHit,
    SearchZones,
    Store,
)


def _b64e(b: bytes | None) -> str | None:
    return None if b is None else base64.b64encode(b).decode("ascii")


def _b64d(s: str | None) -> bytes | None:
    return None if s is None else base64.b64decode(s)


def _row_to_wire(r: CompletedRow) -> dict[str, Any]:
    return {"quest_id": r.quest_id, "status": r.status, "step": r.step,
            "last": r.last, "title": r.title, "created": r.created}


def _row_from_wire(d: dict[str, Any]) -> CompletedRow:
    return CompletedRow(**d)


def _readiness_to_wire(r: Readiness) -> dict[str, Any]:
    return {"ready": r.ready, "blocked_on": list(r.blocked_on)}


def _readiness_from_wire(d: dict[str, Any]) -> Readiness:
    return Readiness(d["ready"], tuple(d["blocked_on"]))


def _home_to_wire(h: HomeDigest) -> dict[str, Any]:
    return {"quest_id": h.quest_id, "quest_json": _b64e(h.quest_json),
            "state_json": _b64e(h.state_json), "meta": h.meta,
            "last_iso": h.last_iso, "has_user_message": h.has_user_message,
            "prose": h.prose, "intent_title": h.intent_title}


def _home_from_wire(d: dict[str, Any]) -> HomeDigest:
    return HomeDigest(
        d["quest_id"], base64.b64decode(d["quest_json"]), _b64d(d["state_json"]),
        d["meta"], d["last_iso"], d["has_user_message"], d["prose"],
        d["intent_title"])


def _card_to_wire(c: QuestCard | None) -> dict[str, Any] | None:
    if c is None:
        return None
    return {"quest_id": c.quest_id, "status": c.status, "step": c.step,
            "last": c.last, "title": c.title, "created": c.created,
            "step_label": c.step_label, "phase_label": c.phase_label,
            "base_branch": c.base_branch, "merge_commit": c.merge_commit,
            "depends_on": list(c.depends_on), "intent": c.intent,
            "summary": _b64e(c.summary)}


def _card_from_wire(d: dict[str, Any] | None) -> QuestCard | None:
    if d is None:
        return None
    return QuestCard(
        quest_id=d["quest_id"], status=d["status"], step=d["step"],
        last=d["last"], title=d["title"], created=d["created"],
        step_label=d["step_label"], phase_label=d["phase_label"],
        base_branch=d["base_branch"], merge_commit=d["merge_commit"],
        depends_on=list(d["depends_on"]), intent=d["intent"],
        summary=_b64d(d["summary"]))


def _hit_to_wire(h: SearchHit) -> dict[str, Any]:
    return {"quest_id": h.quest_id, "title": h.title, "zone": h.zone,
            "content": h.content, "last_activity": h.last_activity}


def _hit_from_wire(d: dict[str, Any]) -> SearchHit:
    return SearchHit(**d)


def _zones_to_wire(z: SearchZones) -> dict[str, Any]:
    return {"active": z.active, "backlog": z.backlog,
            "statuses": sorted(z.statuses), "merged_after": z.merged_after}


def _zones_from_wire(d: dict[str, Any]) -> SearchZones:
    return SearchZones(d["active"], d["backlog"], frozenset(d["statuses"]),
                       d["merged_after"])


def run_query(store: Store, op: str, args: dict[str, Any]) -> Any:
    if op == "get_events":
        return store.get_events(args["quest_id"])
    if op == "get_artifact":
        return _b64e(store.get_artifact(args["quest_id"], args["kind"]))
    if op == "completed_rows":
        return [_row_to_wire(r) for r in store.completed_rows()]
    if op == "backlog_readiness":
        return _readiness_to_wire(store.backlog_readiness(args["depends_on"]))
    if op == "scan_homes":
        return [_home_to_wire(h) for h in store.scan_homes()]
    if op == "quest_card":
        return _card_to_wire(store.quest_card(args["quest_id"]))
    if op == "search":
        return [_hit_to_wire(h)
                for h in store.search(args["query"], _zones_from_wire(args["zones"]))]
    raise ValueError(f"unknown store op {op!r}")
