# SPDX-License-Identifier: MIT
# Copyright 2026 Josh Poimboeuf
"""Best-effort reader of Claude Code's local model cache (spec: roster discovery).

Reads `additionalModelOptionsCache` (a list of dicts; only each entry's `value`
is used) from Claude Code's user config JSON. The config directory is
$CLAUDE_CONFIG_DIR when
set, else the home directory; the file is `.claude.json` inside it. Any failure —
missing file, unreadable, bad JSON, missing/oddly-shaped key, non-string fields —
yields an empty list, never an exception. No network access is made."""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class ClaudeModelOption:
    value: str


def _config_path() -> Path:
    base = os.environ.get("CLAUDE_CONFIG_DIR")
    root = Path(base) if base else Path.home()
    return root / ".claude.json"


def read_claude_model_options() -> list[ClaudeModelOption]:
    try:
        raw = json.loads(_config_path().read_text())
        entries = raw["additionalModelOptionsCache"]
    except (OSError, ValueError, KeyError, TypeError, RuntimeError):
        return []
    if not isinstance(entries, list):
        return []
    out: list[ClaudeModelOption] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        value = entry.get("value")
        if not isinstance(value, str) or not value:
            continue
        out.append(ClaudeModelOption(value=value))
    return out
