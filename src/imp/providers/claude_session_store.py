# SPDX-License-Identifier: MIT
# Copyright 2026 Josh Poimboeuf
"""imp-backed claude SessionStore adapter (design §1).

Persists claude-p session entries under the quest so a resumed step can drive
claude-p's NATIVE session resume from imp-owned bytes — never from
~/.claude/projects/. One JSONL per SessionKey under
`<state dir>/quests/<id>/steps/<step>/claude_session/`; subagent transcripts ride
`subpath` keys. Only `append`/`load` are required by the SDK; `delete` and
`list_subkeys` (added in later tasks) cover lifecycle + subagent discovery on
resume. The SDK duck-types the adapter (no isinstance check), so this class need
not subclass the SDK's `SessionStore` Protocol.
"""
from __future__ import annotations

import datetime
import json
import re
import shutil
import uuid
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from imp.agents.runner import (
    Message,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
)


def _sanitize(value: str) -> str:
    out = re.sub(r"[^A-Za-z0-9._-]", "_", value)
    # A dot-only segment ('.'/'..') survives the class above but would walk the
    # path out of root; neutralize it.
    if set(out) <= {"."}:
        out = out.replace(".", "_")
    return out


def _sanitize_subpath(value: str) -> str:
    return "/".join(_sanitize(seg) for seg in value.split("/") if seg)


class ImpSessionStore:
    def __init__(self, root: Path) -> None:
        self._root = root

    def _base(self, key: Mapping[str, Any]) -> Path:
        return (
            self._root
            / _sanitize(key["project_key"])
            / _sanitize(key["session_id"])
        )

    def _path(self, key: Mapping[str, Any]) -> Path:
        subpath = key.get("subpath")
        if subpath:
            return self._base(key) / f"{_sanitize_subpath(subpath)}.jsonl"
        return self._base(key) / "main.jsonl"

    def _read(self, path: Path) -> list[dict[str, Any]]:
        if not path.exists():
            return []
        out: list[dict[str, Any]] = []
        for line in path.read_text().splitlines():
            line = line.strip()
            if line:
                out.append(json.loads(line))
        return out

    async def append(
        self, key: Mapping[str, Any], entries: list[dict[str, Any]],
    ) -> None:
        if not entries:
            return
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        seen = {e["uuid"] for e in self._read(path) if "uuid" in e}
        with path.open("a") as f:
            for entry in entries:
                uid = entry.get("uuid")
                if uid is not None and uid in seen:
                    continue
                f.write(json.dumps(entry) + "\n")
                if uid is not None:
                    seen.add(uid)

    async def overwrite(
        self, key: Mapping[str, Any], entries: list[dict[str, Any]],
    ) -> None:
        """Replace the key's file with `entries`, truncating. Unlike append (which
        only dedups-and-adds), this drops entries no longer present — the resumed
        body is a whole-transcript projection, so a record the CLI wrote must not
        survive under a uuid the projection reuses."""
        path = self._path(key)
        if not entries:
            path.unlink(missing_ok=True)
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w") as f:
            for entry in entries:
                f.write(json.dumps(entry) + "\n")

    async def load(
        self, key: Mapping[str, Any],
    ) -> list[dict[str, Any]] | None:
        path = self._path(key)
        if not path.exists():
            return None
        return self._read(path)

    async def delete(self, key: Mapping[str, Any]) -> None:
        if key.get("subpath"):
            self._path(key).unlink(missing_ok=True)
            return
        shutil.rmtree(self._base(key), ignore_errors=True)

    def clear(self) -> None:
        # sync lifecycle helper; no-op when the root is absent
        shutil.rmtree(self._root, ignore_errors=True)

    async def list_subkeys(self, key: Mapping[str, Any]) -> list[str]:
        base = self._base(key)
        if not base.exists():
            return []
        out: list[str] = []
        for p in base.rglob("*.jsonl"):
            if p.name == "main.jsonl" and p.parent == base:
                continue
            out.append(p.relative_to(base).with_suffix("").as_posix())
        return sorted(out)


# Hash input, not an identifier: re-seeding derives new ids and orphans every
# existing session transcript.
_SESSION_ID_NAMESPACE = uuid.uuid5(uuid.NAMESPACE_URL, "imp/claude_code/session")


def resume_session_id(agent_id: str) -> str:
    """Deterministic valid-UUID session id, stable across runs (SDK needs a valid UUID)."""
    return str(uuid.uuid5(_SESSION_ID_NAMESPACE, agent_id))


def session_project_key(project_root: Path) -> str:
    """The SDK's project_key for a directory — matches what the SDK mirrors/resumes under."""
    from claude_agent_sdk import project_key_for_directory

    return project_key_for_directory(project_root)


def session_key(project_root: Path, agent_id: str) -> dict[str, str]:
    return {
        "project_key": session_project_key(project_root),
        "session_id": resume_session_id(agent_id),
    }


def session_store_for(transcript_dir: Path) -> ImpSessionStore:
    return ImpSessionStore(transcript_dir / "claude_session")


# Hash input, not an identifier: re-seeding breaks append()'s record dedupe.
_RECORD_UUID_NAMESPACE = uuid.uuid5(uuid.NAMESPACE_URL, "imp/claude_code/session-record")


def _record_uuid(session_id: str, index: int) -> str:
    """Deterministic uuid (session_id + turn index) so a rebuilt body keeps stable record ids."""
    return str(uuid.uuid5(_RECORD_UUID_NAMESPACE, f"{session_id}:{index}"))


def _session_block_to_dict(block: Any) -> dict[str, Any]:
    if isinstance(block, TextBlock):
        return {"type": "text", "text": block.text}
    if isinstance(block, ToolUseBlock):
        return {"type": "tool_use", "id": block.id,
                "name": block.name, "input": block.input}
    if isinstance(block, ToolResultBlock):
        return {"type": "tool_result", "tool_use_id": block.tool_use_id,
                "content": block.content, "is_error": block.is_error}
    raise TypeError(f"unexpected content block: {type(block).__name__}")


def message_to_session_records(
    messages: list[Message], *, session_id: str, cwd: str, model: str,
) -> list[dict[str, Any]]:
    """Convert imp transcript Messages into claude-p on-disk session records.

    Shape matches a real ~/.claude/projects/<key>/<session>.jsonl line: a
    parentUuid->uuid chain carrying sessionId, type, and an Anthropic message.
    The body is a whole-transcript projection, so the chain roots at None.
    """
    records: list[dict[str, Any]] = []
    parent: str | None = None
    timestamp = datetime.datetime.now(datetime.UTC).isoformat()
    for index, m in enumerate(messages):
        rec_uuid = _record_uuid(session_id, index)
        content = [_session_block_to_dict(b) for b in m.content]
        if m.role == "assistant":
            message: dict[str, Any] = {
                "id": "msg_" + rec_uuid.replace("-", "")[:24],
                "type": "message",
                "role": "assistant",
                "model": model,
                "content": content,
                "stop_reason": "end_turn",
                "stop_sequence": None,
                "usage": {"input_tokens": 0, "output_tokens": 0},
            }
        else:
            message = {"role": m.role, "content": content}
        records.append({
            "parentUuid": parent,
            "uuid": rec_uuid,
            "sessionId": session_id,
            "type": m.role,
            "cwd": cwd,
            "isSidechain": False,
            "userType": "external",
            "timestamp": timestamp,
            "message": message,
        })
        parent = rec_uuid
    return records
