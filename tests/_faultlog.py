"""Readers for the diagnostics read-fault log written by imp.engine.readfault."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from imp.paths import imp_home


def diag() -> Path:
    return imp_home() / "diagnostics"


def fault_lines() -> list[dict[str, Any]]:
    log = diag() / "read-faults.jsonl"
    if not log.exists():
        return []
    return [json.loads(ln) for ln in log.read_text().splitlines() if ln.strip()]
