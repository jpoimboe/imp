# SPDX-License-Identifier: MIT
# Copyright 2026 Josh Poimboeuf
"""Wire codec + socket transport for the read-only introspection Store.

`run_query` is the daemon-side dispatcher (one op -> one LocalStore call ->
JSON-safe value). `ProxyStore` is the in-sandbox transport that ships the same
ops to the daemon over the session socket. Bytes-valued fields are base64
strings on the wire; everything else is JSON-native."""
from __future__ import annotations

import base64
from typing import Any, cast

import anyio

from imp.cli.store import (
    CompletedRow,
    HomeDigest,
    NeighbourQuota,
    QuestCard,
    QuotaNeighboursUnsupported,
    Readiness,
    SearchHit,
    SearchZones,
    Store,
)
from imp.remote.session.protocol import encode_query, parse_result


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
            "prose": h.prose, "intent_title": h.intent_title,
            "lock_live": h.lock_live}


def _home_from_wire(d: dict[str, Any]) -> HomeDigest:
    return HomeDigest(
        d["quest_id"], base64.b64decode(d["quest_json"]), _b64d(d["state_json"]),
        d["meta"], d["last_iso"], d["has_user_message"], d["prose"],
        d["intent_title"], d["lock_live"])


def _card_to_wire(c: QuestCard | None) -> dict[str, Any] | None:
    if c is None:
        return None
    return {"quest_id": c.quest_id, "status": c.status,
            "pause_reason": c.pause_reason, "step": c.step,
            "last": c.last, "title": c.title, "created": c.created,
            "step_label": c.step_label, "phase_label": c.phase_label,
            "workflow": c.workflow,
            "base_branch": c.base_branch, "merge_commit": c.merge_commit,
            "parent": c.parent,
            "depends_on": list(c.depends_on), "intent": c.intent,
            "summary": _b64e(c.summary), "vars": dict(c.vars)}


def _card_from_wire(d: dict[str, Any] | None) -> QuestCard | None:
    if d is None:
        return None
    return QuestCard(
        quest_id=d["quest_id"], status=d["status"],
        pause_reason=d["pause_reason"], step=d["step"],
        last=d["last"], title=d["title"], created=d["created"],
        step_label=d["step_label"], phase_label=d["phase_label"],
        workflow=d["workflow"],
        base_branch=d["base_branch"], merge_commit=d["merge_commit"],
        parent=d["parent"],
        depends_on=list(d["depends_on"]), intent=d["intent"],
        summary=_b64d(d["summary"]), vars=dict(d["vars"]))


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


def _neighbour_to_wire(n: NeighbourQuota) -> dict[str, Any]:
    return {"project": n.project, "quest_id": n.quest_id,
            "readings": list(n.readings), "usage": list(n.usage),
            "anchors": list(n.anchors),
            "agents": {a: list(t) for a, t in n.agents.items()}}


def _neighbour_from_wire(d: dict[str, Any]) -> NeighbourQuota:
    return NeighbourQuota(
        project=d["project"], quest_id=d["quest_id"],
        readings=tuple(d["readings"]), usage=tuple(d["usage"]),
        anchors=tuple(d["anchors"]),
        agents={a: (t[0], t[1], t[2]) for a, t in d["agents"].items()})


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
    if op == "search_event_candidates":
        return [list(t) for t in store.search_event_candidates(args["query"])]
    if op == "quota_neighbours":
        return [_neighbour_to_wire(n) for n in store.quota_neighbours(
            args["quest_id"], args["start_iso"], args["end_iso"])]
    raise ValueError(f"unknown store op {op!r}")


class ProxyStore:
    """Ships each Store op to the quest daemon over its session socket.

    One connection per call: send the query line, read the reply to EOF (the
    daemon closes after the single result frame -- no frame-size cap needed),
    decode. Runs only in-sandbox, where the socket is guaranteed present."""

    def __init__(self, path: str) -> None:
        self._path = path

    async def _roundtrip(self, op: str, args: dict[str, Any]) -> Any:
        stream = await anyio.connect_unix(self._path)
        try:
            await stream.send((encode_query(op, args) + "\n").encode("utf-8"))
            chunks: list[bytes] = []
            try:
                while True:
                    chunks.append(await stream.receive())
            except (anyio.EndOfStream, anyio.ClosedResourceError,
                    anyio.BrokenResourceError):
                pass
            return parse_result(b"".join(chunks).decode("utf-8").rstrip("\n"))
        finally:
            await stream.aclose()

    def _call(self, op: str, args: dict[str, Any]) -> Any:
        return anyio.run(self._roundtrip, op, args)

    def get_events(self, quest_id: str) -> list[dict[str, Any]] | None:
        return cast(
            "list[dict[str, Any]] | None",
            self._call("get_events", {"quest_id": quest_id}))

    def get_artifact(self, quest_id: str, kind: str) -> bytes | None:
        return _b64d(self._call("get_artifact", {"quest_id": quest_id, "kind": kind}))

    def completed_rows(self) -> list[CompletedRow]:
        return [_row_from_wire(d) for d in self._call("completed_rows", {})]

    def backlog_readiness(self, depends_on: list[str]) -> Readiness:
        return _readiness_from_wire(
            self._call("backlog_readiness", {"depends_on": depends_on}))

    def scan_homes(self) -> list[HomeDigest]:
        return [_home_from_wire(d) for d in self._call("scan_homes", {})]

    def quest_card(self, quest_id: str) -> QuestCard | None:
        return _card_from_wire(self._call("quest_card", {"quest_id": quest_id}))

    def search(self, query: str, zones: SearchZones) -> list[SearchHit]:
        return [_hit_from_wire(d) for d in self._call(
            "search", {"query": query, "zones": _zones_to_wire(zones)})]

    def search_event_candidates(self, query: str) -> list[tuple[str, str, str, str | None]]:
        return [tuple(x) for x in self._call("search_event_candidates", {"query": query})]

    def quota_neighbours(self, quest_id: str, start_iso: str,
                         end_iso: str) -> list[NeighbourQuota]:
        try:
            rows = self._call("quota_neighbours", {
                "quest_id": quest_id, "start_iso": start_iso, "end_iso": end_iso})
        except RuntimeError as exc:
            # A quest in flight across this upgrade has a pre-upgrade daemon; the
            # text is all parse_result leaves of run_query's ValueError.
            if "unknown store op" in str(exc):
                raise QuotaNeighboursUnsupported(str(exc)) from exc
            raise
        return [_neighbour_from_wire(d) for d in rows]
