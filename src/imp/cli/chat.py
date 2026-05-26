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

import json
import sys
from collections.abc import Callable
from typing import Any, TextIO

# Spinner is a no-op factory by default; the real implementation comes from
# prompt_toolkit in Task 10. Factory returns a stop-callable.
SpinnerFactory = Callable[[str], Callable[[], None]]


def _default_spinner_factory(_msg: str) -> Callable[[], None]:
    # Real spinner injected by main(); tests pass a no-op.
    return lambda: None


def _is_structured_response(text: str) -> bool:
    """Detect a response_format JSON envelope so the renderer doesn't show
    `[agent] {"status":...}` next to the prettier `[out  ] status=...` line.

    Every output schema requires `status`, so a JSON object with that key
    is almost certainly the structured response carrier.
    """
    stripped = text.strip()
    if not (stripped.startswith("{") and stripped.endswith("}")):
        return False
    try:
        parsed = json.loads(stripped)
    except (ValueError, TypeError):
        return False
    return isinstance(parsed, dict) and "status" in parsed


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
        # Tokens accumulate in this buffer until the segment closes (a non-token
        # event arrives). On close we decide whether to flush (real agent text)
        # or discard (response_format JSON envelope — appears as agent_token
        # because Anthropic streams structured output as text tokens).
        self._token_buffer: list[str] = []
        # Tracks whether the current segment is the structured response;
        # set when we render it to suppress the duplicate agent_message.
        self._suppress_next_agent_message = False

    def handle(self, event: dict[str, Any]) -> None:
        t = event["type"]
        p = event.get("payload", {})
        if t == "agent_token":
            self._token_buffer.append(p.get("text", ""))
            return
        # Any non-token event closes the in-flight token segment
        self._flush_token_buffer()
        # Stop a spinner if a tool result arrived
        if t == "tool_result":
            self._stop_active_spinner()

        if t == "agent_message":
            if self._suppress_next_agent_message:
                self._suppress_next_agent_message = False
                return
            text = str(p.get("text", "")).strip()
            if text and not _is_structured_response(text):
                self._line(f"[agent] {text}")
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
            rule = p.get("rule", p.get("gate_type", "?"))
            action = p.get("action", "?")
            self._line(f"[gate ] {rule} -> {action}")
            return
        if t == "script_gate_result":
            exit_code = p.get("exit", "?")
            self._line(f"[gate ] script exit={exit_code}")
            return
        if t in ("step_resumed_with_cached_output", "step_exhausted",
                 "phase_exhausted"):
            step = p.get("step", "")
            phase = p.get("phase", "")
            target = step or phase or "?"
            self._line(f"[wf   ] {t} {target}")
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
        self._flush_token_buffer()
        self._stop_active_spinner()

    # --- helpers ---

    def _flush_token_buffer(self) -> None:
        """Render the accumulated token segment, unless it's the
        response_format JSON envelope (in which case discard — the engine
        will emit a clean agent_output line for the parsed form)."""
        if not self._token_buffer:
            return
        text = "".join(self._token_buffer)
        self._token_buffer.clear()
        if _is_structured_response(text):
            # Suppress the matching agent_message that will arrive in
            # updates-mode for this same content.
            self._suppress_next_agent_message = True
            return
        # Real agent text. Emit as one line so tools+text interleave cleanly,
        # and suppress the duplicate agent_message we'll get from updates-mode.
        stripped = text.strip()
        if stripped:
            self._out.write(f"[agent] {stripped}\n")
            self._out.flush()
            self._suppress_next_agent_message = True

    def _stop_active_spinner(self) -> None:
        if self._stop_spinner is not None:
            try:
                self._stop_spinner()
            finally:
                self._stop_spinner = None

    def line(self, text: str) -> None:
        """Public entrypoint for printing a labeled or plain line."""
        self._line(text)

    def _line(self, text: str) -> None:
        self._out.write(text + "\n")
        self._out.flush()

    @staticmethod
    def _compact(args: Any) -> str:
        s = str(args)
        return s if len(s) < 80 else s[:77] + "..."
