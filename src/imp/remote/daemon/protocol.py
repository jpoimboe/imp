"""The hello-frame codec shared by the south-side connector and the daemon hub.

A south connection opens with exactly one hello frame — a single JSON line naming
the Quest — after which every line is a verbatim NDJSON event (connector → hub) or
a plain reply line (hub → connector). Only the hello is structured here; everything
else is forwarded uninterpreted (spec Decision 3)."""
from __future__ import annotations

import json


def make_hello(quest_id: str, title: str, autosubscribe: bool = False) -> str:
    """The opening frame a connector sends after dialing: a single JSON line (no
    trailing newline — the caller frames it) identifying the Quest."""
    return json.dumps({
        "type": "hello", "quest_id": quest_id, "title": title,
        "autosubscribe": autosubscribe})


def parse_hello(line: str) -> tuple[str, str, bool]:
    """Recover (quest_id, title, autosubscribe) from a hello line (newline already stripped)."""
    obj = json.loads(line)
    if obj.get("type") != "hello":
        raise ValueError(f"expected hello frame, got {obj.get('type')!r}")
    return obj["quest_id"], obj["title"], bool(obj.get("autosubscribe", False))
