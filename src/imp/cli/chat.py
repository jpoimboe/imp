"""ChatRenderer: stream agent events to a terminal-like output stream.

Owns the rendering policy for every event type the engine + runner produce.
Spec §8.3's REPL state machine:
  idle → streaming (agent_token) → idle (agent_message)
       → tool_call → tool_result → idle
       → user_input → streaming ...
       → gate_prompt → idle

The renderer is signal-agnostic — Task 11+12 install the signal handlers
that may cause the underlying agent to stop emitting. Renderer just renders
what arrives.
"""

from __future__ import annotations

import sys
from collections.abc import Callable
from typing import Any, TextIO


# Spinner is a no-op factory by default; the real implementation comes from
# prompt_toolkit in Task 10. Factory returns a stop-callable.
SpinnerFactory = Callable[[str], Callable[[], None]]


def _default_spinner_factory(_msg: str) -> Callable[[], None]:
    # Real spinner injected by main(); tests pass a no-op.
    return lambda: None


class ChatRenderer:
    def __init__(
        self,
        out: TextIO | None = None,
        *,
        spinner: SpinnerFactory = _default_spinner_factory,
    ) -> None:
        self._out = out or sys.stdout
        self._spinner_factory = spinner
        self._stop_spinner: Callable[[], None] | None = None
        self._token_line_open = False

    def handle(self, event: dict[str, Any]) -> None:
        t = event["type"]
        p = event.get("payload", {})
        if t == "agent_token":
            self._write_token(p.get("text", ""))
            return
        # Any non-token event closes the in-flight token line
        self._close_token_line()
        # Stop a spinner if a tool result arrived
        if t == "tool_result":
            self._stop_active_spinner()

        if t == "agent_message":
            # Already streamed via tokens; suppress duplicate
            return
        if t == "tool_call":
            name = p.get("name", "?")
            args = p.get("args", {})
            self._line(f"[tool ] {name} {self._compact(args)}")
            # Start spinner while waiting for tool_result
            self._stop_spinner = self._spinner_factory(f"{name}...")
            return
        if t == "tool_result":
            res = str(p.get("result", ""))
            short = res if len(res) < 80 else res[:77] + "..."
            self._line(f"[tool ] -> {short}")
            return
        if t == "user_input":
            self._line(f"[user ] {p.get('text', '')}")
            return
        if t == "agent_output":
            pairs = " ".join(f"{k}={v}" for k, v in p.items())
            self._line(f"[out  ] {pairs}")
            return
        if t == "step_started":
            step = p.get("step", "?")
            phase = p.get("phase", "?")
            role = p.get("model_role", "?")
            mode = "interactive" if p.get("interactive") else "auto"
            self._line(f"-- {phase}/{step}  [{mode}, model_role={role}]")
            return
        if t == "step_completed":
            self._line(
                f"   {p.get('step', '?')}  -> {p.get('action', '?')}",
            )
            return
        if t == "phase_started":
            self._line(f"\n=== phase: {p.get('phase', '?')} ===")
            return
        if t == "phase_completed":
            self._line(f"=== {p.get('phase', '?')} -> {p.get('action', '?')} ===\n")
            return
        if t == "gate_evaluated":
            self._line(f"[gate ] {p.get('rule', p.get('gate_type', '?'))} -> {p.get('action', '?')}")
            return
        if t in ("workflow_paused", "workflow_resumed", "workflow_started",
                 "workflow_completed"):
            self._line(f"[wf   ] {t}")
            return
        if t == "error":
            self._line(f"[ERROR] {p.get('message', '?')}")
            return
        # Unknown event — render generically so we never silently drop
        self._line(f"[{t}] {p}")

    def close(self) -> None:
        self._close_token_line()
        self._stop_active_spinner()

    # --- helpers ---

    def _write_token(self, text: str) -> None:
        if not text:
            return
        if not self._token_line_open:
            self._out.write("[agent] ")
            self._token_line_open = True
        self._out.write(text)
        self._out.flush()

    def _close_token_line(self) -> None:
        if self._token_line_open:
            self._out.write("\n")
            self._out.flush()
            self._token_line_open = False

    def _stop_active_spinner(self) -> None:
        if self._stop_spinner is not None:
            try:
                self._stop_spinner()
            finally:
                self._stop_spinner = None

    def _line(self, text: str) -> None:
        self._out.write(text + "\n")
        self._out.flush()

    @staticmethod
    def _compact(args: Any) -> str:
        s = str(args)
        return s if len(s) < 80 else s[:77] + "..."
