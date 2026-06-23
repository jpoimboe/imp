"""imp-backed claude_code SessionStore adapter (design §1).

Persists claude-p session entries under the change so a resumed step can drive
claude-p's NATIVE session resume from imp-owned bytes — never from
~/.claude/projects/. One JSONL per SessionKey under
`.imp/changes/<id>/steps/<step>/claude_session/`; subagent transcripts ride
`subpath` keys. Only `append`/`load` are required by the SDK; `delete` and
`list_subkeys` (added in later tasks) cover lifecycle + subagent discovery on
resume. The SDK duck-types the adapter (no isinstance check), so this class need
not subclass the SDK's `SessionStore` Protocol.
"""
from __future__ import annotations

import json
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any


def _sanitize(value: str) -> str:
    out = re.sub(r"[^A-Za-z0-9._-]", "_", value)
    # A dot-only segment ('.'/'..') survives the class above but would walk the
    # path out of root; neutralize it.
    if set(out) <= {"."}:
        out = out.replace(".", "_")
    return out


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
            return self._base(key) / f"{subpath}.jsonl"
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

    async def load(
        self, key: Mapping[str, Any],
    ) -> list[dict[str, Any]] | None:
        path = self._path(key)
        if not path.exists():
            return None
        return self._read(path)
