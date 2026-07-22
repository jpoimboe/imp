"""Stable record projection + committed-golden compare for the conformance suite."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

GOLDEN_DIR = Path(__file__).parent / "golden" / "conformance"


def record(driven: Any) -> dict[str, Any]:
    """Project a driven scenario to a stable, JSON-serializable record."""
    control = list(driven.control)
    log_events = list(driven.log.read())
    return {
        "terminal": control[-1]["type"] if control else None,
        "control_types": [e["type"] for e in control],
        "agent_result": next(
            (e["payload"] for e in control if e["type"] == "agent_result"), None),
        "errors": [
            [e["payload"]["category"], e["payload"]["kind"]]
            for e in control if e["type"] == "error"],
        "log_types": [e["type"] for e in log_events],
        "step_context_count": sum(
            1 for e in log_events if e["type"] == "step_context"),
        "raised": type(driven.raised).__name__ if driven.raised is not None else None,
    }


def check_golden(name: str, rec: dict[str, Any]) -> None:
    path = GOLDEN_DIR / f"{name}.json"
    actual = json.dumps(rec, indent=2, sort_keys=True) + "\n"
    if os.environ.get("IMP_CONFORMANCE_UPDATE"):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(actual)
        return
    assert path.exists(), (
        f"missing golden {path}; regenerate with IMP_CONFORMANCE_UPDATE=1")
    expected = path.read_text()
    assert actual == expected, (
        f"conformance drift for {name}:\n--- golden ---\n{expected}"
        f"--- actual ---\n{actual}")
