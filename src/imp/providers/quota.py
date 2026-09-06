# SPDX-License-Identifier: MIT
# Copyright 2026 Josh Poimboeuf
"""The normalized, provider-agnostic quota reading.

Every subscription carrier reports an account-wide rate-limit level over one or
two windows. This is that reading with the carrier's shape flattened out:
`used_pct` is always 0-100, `resets_at` always epoch seconds or None, and
`windows` is always ordered by window duration ascending. `raw` carries the
provider's block verbatim and is never read by imp — telemetry not recorded
cannot be backfilled.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal


@dataclass(frozen=True, slots=True)
class QuotaWindow:
    label: str
    used_pct: float
    resets_at: int | None


@dataclass(frozen=True, slots=True)
class QuotaReading:
    """Built only by a provider parser, which yields None rather than a reading
    with an empty `windows`."""

    provider: Literal["codex", "claude_code"]
    plan: str | None
    windows: tuple[QuotaWindow, ...]
    raw: dict[str, Any]

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> QuotaReading:
        """The inverse of to_payload, over a `quota_reading` event this module wrote."""
        return cls(
            provider=payload["provider"],
            plan=payload["plan"],
            windows=tuple(
                QuotaWindow(label=w["label"], used_pct=w["used_pct"],
                            resets_at=w["resets_at"])
                for w in payload["windows"]),
            raw=payload["raw"])

    def to_payload(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "plan": self.plan,
            "windows": [{"label": w.label, "used_pct": w.used_pct,
                         "resets_at": w.resets_at} for w in self.windows],
            "raw": self.raw,
        }
