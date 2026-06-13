"""The hello-frame codec shared by the south-side connector and the daemon hub.

A south connection opens with exactly one hello frame — a single JSON line naming
the Change — after which every line is a verbatim NDJSON event (connector → hub) or
a plain reply line (hub → connector). Only the hello is structured here; everything
else is forwarded uninterpreted (spec Decision 3)."""
from __future__ import annotations

import json


def make_hello(change_id: str, title: str) -> str:
    """The opening frame a connector sends after dialing: a single JSON line (no
    trailing newline — the caller frames it) identifying the Change."""
    return json.dumps({"type": "hello", "change_id": change_id, "title": title})


def parse_hello(line: str) -> tuple[str, str]:
    """Recover (change_id, title) from a hello line (newline already stripped)."""
    obj = json.loads(line)
    return obj["change_id"], obj["title"]
