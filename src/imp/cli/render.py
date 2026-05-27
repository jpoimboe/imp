"""Human-readable renderer for `imp log` / `imp log --full`.

Self-contained: depends only on the event-dict shape used in
``events.jsonl``. No imports from ``imp.engine.*``. Pure functions and one
generator; no global state.

Spec: ``docs/superpowers/specs/2026-05-27-imp-log-readability-design.md``.
"""

from __future__ import annotations

import os
from typing import Any


_ANSI: dict[str, str] = {
    "dim":     "\x1b[2m",
    "bold":    "\x1b[1m",
    "red":     "\x1b[31m",
    "green":   "\x1b[32m",
    "yellow":  "\x1b[33m",
    "blue":    "\x1b[34m",
    "magenta": "\x1b[35m",
    "cyan":    "\x1b[36m",
    "gray":    "\x1b[90m",  # bright-black, not white
    "reset":   "\x1b[0m",
}


def _style(text: str, *codes: str, enabled: bool) -> str:
    """Wrap ``text`` in the named ANSI codes when ``enabled``.

    Returns ``text`` unchanged when disabled. Codes are applied in argument
    order; a single ``reset`` is appended once.
    """
    if not enabled or not codes:
        return text
    prefix = "".join(_ANSI[c] for c in codes)
    return f"{prefix}{text}{_ANSI['reset']}"


def should_use_color(stream: Any) -> bool:
    """Color is enabled iff ``stream.isatty()`` is true and ``NO_COLOR`` is
    unset in the environment. Matches the de-facto ``NO_COLOR`` convention.
    """
    return bool(stream.isatty()) and "NO_COLOR" not in os.environ


# ----------------------------------------------------------------------
# Accent verb colors for action fields on *_completed / *_exhausted events.
# ----------------------------------------------------------------------

_ACCENT: dict[str, str] = {
    "next":      "green",
    "loop":      "green",
    "continue":  "green",
    "retry":     "yellow",
    "pause":     "cyan",
    "goto":      "cyan",
    "exhausted": "red",
}


def _accent_action(action: str, color: bool) -> str:
    """Color the action token per the §5 palette. Unknown actions render
    plain."""
    code = _ACCENT.get(action)
    if code is None:
        return action
    return _style(action, code, enabled=color)


def _arrow(color: bool) -> str:
    return _style("→", "dim", enabled=color)


def _elide(s: str, limit: int) -> str:
    """Truncate to ``limit`` chars, appending the ``…`` token when cut."""
    if len(s) <= limit:
        return s
    return s[:limit] + "…"


# ----------------------------------------------------------------------
# Per-event formatters. Each returns the summary text only (no timestamp,
# no type column, no indent). ``full`` only affects tool_call/tool_result;
# every other formatter ignores it.
# ----------------------------------------------------------------------

def _fmt_change_created(p: dict[str, Any], full: bool, color: bool) -> str:
    return f"#{p.get('change_id', '?')}  {p.get('title', '')}"


def _fmt_workflow_started(p: dict[str, Any], full: bool, color: bool) -> str:
    name = p.get("name", "?")
    frm = p.get("from_phase")
    return f"{name}  (from {frm})" if frm else str(name)


def _fmt_workflow_paused(p: dict[str, Any], full: bool, color: bool) -> str:
    reason = p.get("reason")
    return f"paused  ({reason})" if reason else "paused"


def _fmt_workflow_resumed(p: dict[str, Any], full: bool, color: bool) -> str:
    return "resumed"


def _fmt_phase_started(p: dict[str, Any], full: bool, color: bool) -> str:
    return str(p.get("phase", "?"))


def _fmt_phase_completed(p: dict[str, Any], full: bool, color: bool) -> str:
    phase = p.get("phase", "?")
    action = str(p.get("action", "?"))
    return f"{phase}  {_arrow(color)} {_accent_action(action, color)}"


def _fmt_phase_exhausted(p: dict[str, Any], full: bool, color: bool) -> str:
    phase = p.get("phase", "?")
    return f"{phase}  {_arrow(color)} {_accent_action('exhausted', color)}"


def _fmt_step_started(p: dict[str, Any], full: bool, color: bool) -> str:
    step = p.get("step", "?")
    role = p.get("model_role", "default")
    interactive = bool(p.get("interactive", False))
    out = str(step)
    if role and role != "default":
        out = f"{out}  [{role}]"
    if interactive:
        out = f"{out} (interactive)"
    return out


def _fmt_step_completed(p: dict[str, Any], full: bool, color: bool) -> str:
    step = p.get("step", "?")
    action = str(p.get("action", "?"))
    return f"{step}  {_arrow(color)} {_accent_action(action, color)}"


def _fmt_step_exhausted(p: dict[str, Any], full: bool, color: bool) -> str:
    step = p.get("step", "?")
    return f"{step}  {_arrow(color)} {_accent_action('exhausted', color)}"


def _fmt_step_resumed_with_cached_output(
    p: dict[str, Any], full: bool, color: bool,
) -> str:
    return f"{p.get('step', '?')}  (cached)"


def _fmt_gate_evaluated(p: dict[str, Any], full: bool, color: bool) -> str:
    # Legacy shape: {phase|step, gate_type, action: <str>}
    if "gate_type" in p:
        target = p.get("phase") or p.get("step") or "?"
        action = str(p.get("action", "?"))
        return (
            f"{p['gate_type']} on {target}  "
            f"{_arrow(color)} {_accent_action(action, color)}"
        )
    # Modern shape: {kind, gate?, action: {kind, target?}, ...}
    kind = str(p.get("kind", "?"))
    action_d = p.get("action") or {}
    if isinstance(action_d, dict):
        a_kind = str(action_d.get("kind", "?"))
        if a_kind == "goto" and "target" in action_d:
            # Apply the cyan accent to the whole "goto(target)" token.
            action_styled = _style(
                f"goto({action_d['target']})", "cyan", enabled=color,
            )
        else:
            action_styled = _accent_action(a_kind, color)
    else:
        action_styled = _accent_action(str(action_d), color)
    label = f"{p['gate']} ({kind})" if "gate" in p else f"{kind} gate"
    return f"{label}  {_arrow(color)} {action_styled}"


def _fmt_script_gate_result(p: dict[str, Any], full: bool, color: bool) -> str:
    rc = p.get("exit", "?")
    if rc == 0:
        return f"{_arrow(color)} exit={rc}"
    stderr = (p.get("stderr") or "").splitlines()
    first_line = stderr[0] if stderr else ""
    if first_line:
        return f"{_arrow(color)} exit={rc}  {_elide(first_line, 60)}"
    return f"{_arrow(color)} exit={rc}"


def _fmt_agent_output(p: dict[str, Any], full: bool, color: bool) -> str:
    items = list(p.items())[:2]
    return " ".join(f"{k}={_elide(str(v), 60)}" for k, v in items)


def _fmt_error(p: dict[str, Any], full: bool, color: bool) -> str:
    return _elide(str(p.get("message", "?")), 80)


def _fmt_tools(p: dict[str, Any], full: bool, color: bool) -> str:
    counts = p.get("counts") or {}
    total = sum(counts.values())
    by_count = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    parts = ", ".join(f"{n} {name}" for name, n in by_count)
    return f"{total} calls  ({parts})"


def _fmt_unknown(p: dict[str, Any], full: bool, color: bool) -> str:
    if not isinstance(p, dict):
        return _elide(repr(p), 80)
    return " ".join(f"{k}={_elide(str(v), 60)}" for k, v in p.items())


def _fmt_malformed(event: Any, full: bool, color: bool) -> str:
    return _elide(repr(event), 80)


# ----------------------------------------------------------------------
# Tool-call name resolver. Used in full mode to label tool_result lines.
# ----------------------------------------------------------------------

def _record_tool_call(payload: dict[str, Any], names: dict[str, str]) -> None:
    """Record ``tool_call_id → tool_name`` so subsequent ``tool_result``
    events can be labeled."""
    tcid = payload.get("id")
    name = payload.get("name")
    if isinstance(tcid, str) and isinstance(name, str):
        names[tcid] = name


def _lookup_tool_name(tcid: Any, names: dict[str, str]) -> str | None:
    if isinstance(tcid, str):
        return names.get(tcid)
    return None


def _fmt_tool_call(p: dict[str, Any], full: bool, color: bool) -> str:
    name = p.get("name", "tool")
    args = p.get("args") or p.get("input") or {}
    if isinstance(args, dict):
        parts = " ".join(f"{k}={_elide(str(v), 60)}" for k, v in args.items())
    else:
        parts = _elide(str(args), 60)
    return f"{name}  ({parts})"


def _fmt_tool_result_with_name(
    p: dict[str, Any], *, tool_name: str | None, full: bool, color: bool,
) -> str:
    raw = p.get("result")
    if raw is None:
        raw = p.get("content", "")
    s = str(raw)
    # Escape newlines so each event is one line.
    escaped = s.replace("\n", "\\n")
    if len(escaped) > 80:
        short = f"{escaped[:80]}… ({len(s)} chars)"
    else:
        short = escaped
    label = tool_name if tool_name else "tool"
    return f"{label} → {short}"
