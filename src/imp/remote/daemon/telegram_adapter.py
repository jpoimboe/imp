"""Telegram north adapter for the remote-control daemon (spec Decision 4)."""
from __future__ import annotations

import re

_MODERN_ID = re.compile(r"^\d{4}-\d{2}-\d{2}-([a-z0-9]{6})$")


def handle_for(change_id: str) -> str:
    """Short routing handle for a change: the 6-char suffix (modern) or the full id (legacy)."""
    m = _MODERN_ID.match(change_id)
    return m.group(1) if m else change_id


def parse_handle(text: str) -> tuple[str | None, str]:
    """Split a leading `@handle` off an inbound reply; returns (handle, rest) or (None, text)."""
    if text.startswith("@"):
        head, _, rest = text.partition(" ")
        return head[1:], rest.lstrip()
    return None, text
