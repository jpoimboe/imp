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


# ----------------------------------------------------------------------
# Burst collapse: default mode only. A "burst" is a maximal run of
# consecutive tool_call / tool_result events occurring inside a
# step_started…step_completed window, with no non-tool event between
# them. Each burst collapses to one synthetic "tools" event timestamped
# at the last raw tool event in the burst.
# ----------------------------------------------------------------------

_TOOL_TYPES = frozenset({"tool_call", "tool_result"})


def _collapse_tool_bursts(events):
    """Generator wrapper over an event iterator.

    Stateful: tracks an ``in_step`` flag toggled by ``step_started`` /
    ``step_completed`` / ``step_exhausted`` events, and an in-progress
    burst buffer (counts + last-timestamp). Any non-tool event flushes
    the burst.
    """
    in_step = False
    counts: dict[str, int] = {}
    last_ts: str | None = None

    def _flush():
        nonlocal counts, last_ts
        if counts:
            ev = {
                "timestamp": last_ts,
                "type": "tools",
                "payload": {"counts": dict(counts)},
            }
            counts = {}
            last_ts = None
            return ev
        counts = {}
        last_ts = None
        return None

    for e in events:
        t = e.get("type")
        if t in _TOOL_TYPES and in_step:
            if t == "tool_call":
                name = (e.get("payload") or {}).get("name") or "?"
                counts[name] = counts.get(name, 0) + 1
            last_ts = e.get("timestamp", last_ts)
            continue
        # Any non-tool event (or any tool event outside a step window)
        # flushes the burst.
        flushed = _flush()
        if flushed is not None:
            yield flushed
        if t == "step_started":
            in_step = True
        elif t in ("step_completed", "step_exhausted"):
            in_step = False
        yield e
    # End-of-stream: flush any pending burst.
    flushed = _flush()
    if flushed is not None:
        yield flushed


# ----------------------------------------------------------------------
# Depth (indentation) and color category per event type. Per spec §6.2:
#   0 spaces — workflow + phase events
#   2 spaces — step + gate events
#   4 spaces — tool / output / error events
# ----------------------------------------------------------------------

_DEPTH: dict[str, int] = {
    "change_created":   0,
    "workflow_started": 0,
    "workflow_paused":  0,
    "workflow_resumed": 0,
    "phase_started":    0,
    "phase_completed":  0,
    "phase_exhausted":  0,
    "step_started":     2,
    "step_completed":   2,
    "step_exhausted":   2,
    "step_resumed_with_cached_output": 2,
    "gate_evaluated":   2,
    "script_gate_result": 2,
    "tool_call":        4,
    "tool_result":      4,
    "tools":            4,
    "agent_output":     4,
    "error":            4,
}


# Color codes applied to the event-type column (in addition to the
# accent-verb colors inside summaries). Returned as a tuple so multiple
# codes can be composed (e.g. ``("bold", "magenta")``).
_TYPE_COLOR: dict[str, tuple[str, ...]] = {
    "change_created":   ("bold", "magenta"),
    "workflow_started": ("bold", "magenta"),
    "workflow_resumed": ("bold", "magenta"),
    "workflow_paused":  ("yellow",),
    "phase_started":    ("bold", "blue"),
    "phase_completed":  ("bold", "blue"),
    "phase_exhausted":  ("bold", "blue"),
    "step_started":     ("cyan",),
    "step_completed":   ("cyan",),
    "step_exhausted":   ("cyan",),
    "step_resumed_with_cached_output": ("cyan",),
    "gate_evaluated":   ("yellow",),
    "script_gate_result": ("yellow",),
    "agent_output":     ("green",),
    "tool_call":        ("gray",),
    "tool_result":      ("gray",),
    "tools":            ("gray",),
    "error":            ("bold", "red"),
}


_FORMATTERS = {
    "change_created":   _fmt_change_created,
    "workflow_started": _fmt_workflow_started,
    "workflow_paused":  _fmt_workflow_paused,
    "workflow_resumed": _fmt_workflow_resumed,
    "phase_started":    _fmt_phase_started,
    "phase_completed":  _fmt_phase_completed,
    "phase_exhausted":  _fmt_phase_exhausted,
    "step_started":     _fmt_step_started,
    "step_completed":   _fmt_step_completed,
    "step_exhausted":   _fmt_step_exhausted,
    "step_resumed_with_cached_output": _fmt_step_resumed_with_cached_output,
    "gate_evaluated":   _fmt_gate_evaluated,
    "script_gate_result": _fmt_script_gate_result,
    "agent_output":     _fmt_agent_output,
    "error":            _fmt_error,
    "tool_call":        _fmt_tool_call,
    "tools":            _fmt_tools,
    # tool_result handled specially in render_log: needs the tool-name map.
}


_TYPE_COL_WIDTH = 18


def _parse_local_dt(iso_ts: str):
    """Parse an ISO-8601 timestamp and convert to local time."""
    from datetime import datetime
    return datetime.fromisoformat(iso_ts).astimezone()


def _pick_ts_format(events: list[dict]) -> str:
    """Return ``%H:%M:%S`` if every event's local date is the same,
    else ``%m-%d %H:%M:%S``."""
    dates = set()
    for e in events:
        ts = e.get("timestamp")
        if not isinstance(ts, str):
            continue
        try:
            dates.add(_parse_local_dt(ts).date())
        except (ValueError, TypeError):
            continue
        if len(dates) > 1:
            return "%m-%d %H:%M:%S"
    return "%H:%M:%S"


def _format_timestamp(iso_ts: Any, fmt: str, color: bool) -> str:
    if not isinstance(iso_ts, str):
        text = "-" * (8 if fmt == "%H:%M:%S" else 14)
        return _style(text, "dim", enabled=color)
    try:
        text = _parse_local_dt(iso_ts).strftime(fmt)
    except (ValueError, TypeError):
        text = "-" * (8 if fmt == "%H:%M:%S" else 14)
    return _style(text, "dim", enabled=color)


def _is_malformed(event: Any) -> bool:
    if not isinstance(event, dict):
        return True
    if "type" not in event:
        return True
    if "payload" in event and not isinstance(event["payload"], dict):
        return True
    return False


def render_log(events, full: bool, color: bool):
    """Yield rendered output lines for ``events``.

    Args:
        events: Iterable of event dicts (as read from ``events.jsonl``).
        full:   If True, render every event verbatim — no burst collapse,
                tool_call/tool_result get their full-mode summaries.
        color:  If True, emit ANSI color codes.

    Yields:
        One ``str`` per event (after burst collapsing in non-full mode).
    """
    buffered = list(events)
    ts_fmt = _pick_ts_format(buffered)

    iterable = buffered if full else _collapse_tool_bursts(buffered)

    tool_names: dict[str, str] = {}

    for e in iterable:
        if _is_malformed(e):
            ts = e.get("timestamp") if isinstance(e, dict) else None
            ts_str = _format_timestamp(ts, ts_fmt, color)
            type_token = "malformed".ljust(_TYPE_COL_WIDTH)
            type_styled = _style(type_token, "bold", "red", enabled=color)
            summary = _style(_fmt_malformed(e, full, color), "red", enabled=color)
            yield f"{ts_str}  {type_styled}  {summary}"
            continue

        t = e["type"]
        p = e.get("payload") or {}
        depth = _DEPTH.get(t, 4)
        indent = " " * depth

        # Maintain the tool-name map regardless of mode — cheap, and useful
        # if we ever expand full-mode features.
        if t == "tool_call":
            _record_tool_call(p, tool_names)

        # Dispatch to formatter
        if t == "tool_result":
            tool_name = _lookup_tool_name(p.get("tool_call_id"), tool_names)
            summary = _fmt_tool_result_with_name(
                p, tool_name=tool_name, full=full, color=color,
            )
        elif t in _FORMATTERS:
            summary = _FORMATTERS[t](p, full, color)
        else:
            summary = _fmt_unknown(p, full, color)

        ts_str = _format_timestamp(e.get("timestamp"), ts_fmt, color)
        # Indent goes BEFORE the type so timestamps + types stay aligned
        # within a depth band (spec §6.1).
        type_field = f"{indent}{t}".ljust(_TYPE_COL_WIDTH)
        type_color = _TYPE_COLOR.get(t)
        if type_color:
            type_styled = _style(type_field, *type_color, enabled=color)
        else:
            type_styled = _style(type_field, "dim", enabled=color)
        yield f"{ts_str}  {type_styled}  {summary}"
