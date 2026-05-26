"""ChatRenderer: stream agent events to a terminal-like output stream.

V1: no streaming; events arrive as discrete agent_message blocks. The
renderer dispatches per event type. Spec §5.3 event vocabulary.
"""

from __future__ import annotations

import json
import sys
from typing import Any, TextIO


def _compact(value: Any) -> str:
    s = json.dumps(value, default=str) if not isinstance(value, str) else value
    return s if len(s) < 80 else s[:77] + "..."


class ChatRenderer:
    def __init__(self, out: TextIO | None = None) -> None:
        self._out = out or sys.stdout

    def handle(self, event: dict[str, Any]) -> None:
        t = event["type"]
        p = event.get("payload", {})

        if t == "agent_message":
            self._render_agent_message(p)
            return
        if t == "tool_call":
            self._line(f"[tool ] {p.get('name', '?')}({_compact(p.get('input', {}))})")
            return
        if t == "tool_result":
            truncated = (p.get("content") or "")[:200]
            err = " (error)" if p.get("error") else ""
            self._line(f"[result] {truncated}{err}")
            return
        if t == "tool_killed":
            self._line(f"[kill ] {p.get('name', 'tool')} terminated")
            return
        if t == "user_input":
            self._line(f"[you  ] {p.get('text', '')}")
            return
        if t == "agent_output":
            kvs = " ".join(f"{k}={v}" for k, v in p.items())
            self._line(f"[out  ] {kvs}")
            return
        if t == "runner_warning":
            self._line(f"[warn ] {p.get('message', '?')}")
            return
        if t == "step_started":
            step = p.get("step", "?")
            model = p.get("model", "?")
            self._line(f"── {step} ── [model={model}]")
            return
        if t == "step_completed":
            self._line(f"── {p.get('step', '?')} done → {p.get('action', '?')}")
            return
        if t == "phase_started":
            self._line(f"\n═══ {p.get('phase', '?')} ═══")
            return
        if t == "phase_completed":
            self._line(f"═══ {p.get('phase', '?')} done ═══\n")
            return
        if t == "gate_evaluated":
            kind = p.get("kind", "?")
            action = p.get("action", "?")
            self._line(f"[gate ] {kind} → {action}")
            return
        if t in ("workflow_paused", "workflow_resumed", "workflow_started",
                 "workflow_completed"):
            self._line(f"[wf   ] {t}")
            return
        if t == "error":
            self._line(f"[err  ] {p.get('message', p.get('kind', '?'))}")
            return
        # Unknown event — render generically so we never silently drop
        self._line(f"[{t}] {p}")

    def _render_agent_message(self, payload: dict[str, Any]) -> None:
        content = payload.get("content", [])
        for block in content:
            kind = block.get("type")
            if kind == "text":
                text = (block.get("text") or "").strip()
                if text:
                    self._line(f"[agent] {text}")
            # tool_use blocks are followed by a tool_call event with the same id,
            # so we don't render them here — the tool_call event handles display.

    def close(self) -> None:
        # No streaming buffer to flush in V1.
        pass

    def line(self, text: str) -> None:
        """Public entrypoint for printing a labeled or plain line."""
        self._line(text)

    def _line(self, text: str) -> None:
        self._out.write(text + "\n")
        self._out.flush()
