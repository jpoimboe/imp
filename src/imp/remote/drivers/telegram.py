"""imp-telegram — Telegram Bot API helpers backing the daemon's north adapter.

Originally a standalone per-run remote driver, this module is now a library: the
per-repo remote-control daemon reaches Telegram entirely through its north adapter
(``imp.remote.daemon.telegram_adapter``), which composes the helpers here —
credential loading, event curation, message formatting, and the single HTTP seam
(`api_call`). imp no longer spawns this module as a subprocess (every configured
driver runs through the daemon-connector), so the old stdin/stdout relay entry
point has been removed.

getUpdates is *outbound only* — no inbound port, tunnel, or hosting.
"""
from __future__ import annotations

import json
import os
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

# Delivery tiers (spec §2). buzz = input prompts + status transitions (phone buzzes);
# silent = narrative / script output / step dividers (streams quietly); everything else drops.
_BUZZ = frozenset({
    "awaiting_user_input", "merge_blocked", "script_blocked", "change_merged",
    "error",
})
_SILENT = frozenset({
    "llm_response", "agent_output", "script_output", "step_started",
    "workflow_resumed", "change_abandoned",
})

# workflow_paused is curated by reason, not static membership. A pause is silent
# when a terminal error companion already buzzed the same stop (or the stop was
# self-inflicted / self-resolving). Static members are ALWAYS silent regardless of
# kind. context_window is intentionally absent: it is dual-source and handled
# kind-aware by _pause_is_silent (reincarnation_cap is companion-less → buzzes).
# Everything not silenced — including any unknown or future reason — buzzes, so a
# genuine companion-less stop is never missed.
_PAUSE_SILENT_REASONS = frozenset({
    "merge_blocked",         # merge_blocked companion buzzed
    "script_blocked",        # script_blocked companion buzzed
    "agent_output_missing",  # error companion buzzed
    "awaiting_turn",         # merge-queue hibernation; self-resolving
    "interrupt",             # esc / ctrl-c — self-inflicted, no companion buzz
    "awaiting_user",         # genuine your-turn park; the "needs input" buzz is the signal
    "api",                   # terminal error companion buzzed
    "quota",                 # terminal error companion buzzed
    "content_filter",        # terminal error companion buzzed
    "loop",                  # terminal error companion buzzed
    "terminated",            # terminal error companion buzzed
    "unknown",               # terminal error companion buzzed
    "config",                # terminal config error companion buzzed
    "internal_error",        # terminal internal error companion buzzed
    "retry_ceiling",         # last-retry terminal error companion buzzed
    "reauth_needed",         # terminal auth error companion buzzed
})

# imp-canon plain-speak bodies for a buzzing workflow_paused (companion-less
# reasons only — silenced reasons return "" from _pause_is_silent before this map
# is consulted). An unmapped reason falls back to `paused (<reason>)`.
_PAUSE_REASON_BODIES = {
    "context_window": "hit the context limit",  # reincarnation-cap stop (companion-less)
    "max_loops": "stuck in a loop",
    "unknown_step": "workflow error",
}

# Human-readable labels for buzz events — phone notifications read as plain English,
# not imp's internal snake_case types. An unmapped type falls back to its raw name.
_EVENT_LABELS = {
    "awaiting_user_input": "needs your input",
    "merge_blocked": "merge blocked",
    "script_blocked": "script blocked",
    "change_merged": "done",
    "change_abandoned": "abandoned",
    "workflow_paused": "paused",
    "workflow_resumed": "resumed",
    "error": "error",
}

_API_BASE = "https://api.telegram.org"


def _api_base() -> str:
    return os.environ.get("IMP_TELEGRAM_API_BASE", _API_BASE)


_LONG_POLL_TIMEOUT = 25   # seconds; getUpdates long-poll window
_HTTP_TIMEOUT = 35        # seconds; urlopen ceiling, must exceed the long-poll

TELEGRAM_MAX_TEXT = 4096
_TRUNCATION_MARKER = "\n[truncated]"


def clamp_text(text: str) -> str:
    if len(text) <= TELEGRAM_MAX_TEXT:
        return text
    keep = TELEGRAM_MAX_TEXT - len(_TRUNCATION_MARKER)
    return text[:keep] + _TRUNCATION_MARKER


def format_time_of_day(iso: str) -> str:
    """Event's UTC timestamp as lower-case am/pm in daemon-local time (its original send time)."""
    dt = datetime.fromisoformat(iso).astimezone()
    hour = dt.hour % 12 or 12
    suffix = "am" if dt.hour < 12 else "pm"
    return f"{hour}:{dt.minute:02d}{suffix}"


def step_divider(event: Mapping[str, Any]) -> str | None:
    """Header-less divider for a step_started event.

    None when the frame is hidden or the label is empty.
    """
    payload = event.get("payload") or {}
    if not payload.get("show_label", True):
        return None
    label = str(payload.get("label") or "").strip()
    if not label:
        return None
    phase = str(payload.get("phase_label") or "").strip()
    inner = f"{phase} › {label}" if phase else label
    return f"— {inner} —"


def header_line(event: Mapping[str, Any]) -> str:
    """The per-message header 'id · title · time'; time is the event's own send
    time. Collapses to 'id · time' when no title exists yet or the title just equals
    the id, so a not-yet-renamed Change never renders 'id · id · time'."""
    change = event.get("change") or {}
    cid = str(change.get("id") or "")
    title = str(change.get("title") or "")
    ts = event.get("timestamp")
    when = format_time_of_day(ts) if ts else ""
    if title and title != cid:
        return f"{cid} · {title} · {when}"
    return f"{cid} · {when}"


def _pause_is_silent(payload: Mapping[str, Any]) -> bool:
    """A workflow_paused is silent when a terminal error companion already buzzed the
    same stop (or the stop was self-inflicted / self-resolving). Sole source of truth
    for the silence decision, shared by curate() and event_body(). context_window is
    dual-source: the Tier B hard-window fatal (a provider kind) is silenced by its
    terminal companion, but the companion-less reincarnation cap
    (kind == "reincarnation_cap") must buzz."""
    reason = payload.get("reason")
    if reason in _PAUSE_SILENT_REASONS:
        return True
    return reason == "context_window" and payload.get("kind") != "reincarnation_cap"


def curate(event_type: Any, payload: Mapping[str, Any]) -> str | None:
    """Classify an event into a delivery tier: 'buzz' (notify), 'silent' (quiet
    stream), or None (dropped). Buzz = input prompts + status transitions; silent =
    narrative / script output / step dividers; everything else (tool plumbing) drops. Two
    payload-aware cases: a nonterminal interrupt 'error' is dropped to match the summary
    builder (only terminal errors buzz); and 'workflow_paused' is curated by its payload
    'reason' — silent when a companion event already buzzed the stop, else buzz."""
    if event_type == "error":
        from imp.cli.render_summary import _is_nonterminal_error
        return None if _is_nonterminal_error(dict(payload)) else "buzz"
    if event_type == "workflow_paused":
        return "silent" if _pause_is_silent(payload) else "buzz"
    if event_type in _BUZZ:
        return "buzz"
    if event_type in _SILENT:
        return "silent"
    return None


class ConfigError(Exception):
    """Raised when required Telegram credentials / allowlist are missing."""


@dataclass(frozen=True)
class TelegramConfig:
    token: str
    chat_id: int


def _parse_kv_file(text: str) -> dict[str, str]:
    """Parse an out-of-tree KEY=VALUE credentials file (one pair per line; '#'
    comments and blank lines ignored)."""
    out: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        out[key.strip()] = value.strip()
    return out


def load_config(env: Mapping[str, str], file_text: str | None = None) -> TelegramConfig:
    """Resolve the bot token + allowlisted chat_id from the environment, falling
    back to an out-of-tree KEY=VALUE file (env wins). Refuses without an explicit
    chat_id allowlist (spec AC 11): only that chat may drive the agent.
    Credentials never come from the repo."""
    file_vals = _parse_kv_file(file_text) if file_text else {}
    token = env.get("IMP_TELEGRAM_TOKEN") or file_vals.get("IMP_TELEGRAM_TOKEN")
    chat_raw = env.get("IMP_TELEGRAM_CHAT_ID") or file_vals.get("IMP_TELEGRAM_CHAT_ID")
    if not chat_raw:
        raise ConfigError(
            "IMP_TELEGRAM_CHAT_ID is required: refusing to start without an "
            "explicit chat allowlist")
    try:
        chat_id = int(chat_raw)
    except ValueError as exc:
        raise ConfigError(
            f"IMP_TELEGRAM_CHAT_ID must be an integer, got {chat_raw!r}") from exc
    if not token:
        raise ConfigError("IMP_TELEGRAM_TOKEN is required")
    return TelegramConfig(token=token, chat_id=chat_id)


def _llm_response_text(payload: Mapping[str, Any]) -> str:
    """Render the assistant's prose from an llm_response payload. The text lives
    in payload['raw'] in a provider-specific shape, so reuse imp's own
    provider-aware renderer (the single source of truth shared with `imp log` and
    the live chat). Imported lazily so this module stays import-light, and guarded
    so a parse failure degrades to an empty body instead of crashing."""
    try:
        from imp.cli.render_helpers import render_llm_response
        rendered = render_llm_response(dict(payload))
    except Exception:
        return ""
    return "\n".join(t for t in rendered.texts if t and t.strip()).strip()


def event_body(event_type: Any, payload: Mapping[str, Any]) -> str:
    """Extract the human-readable body for one event. Events carry type-specific
    payload schemas (not a uniform 'text' field), so map the known ones; fall back
    to the generic text/message keys for everything else."""
    if event_type == "awaiting_user_input":
        return str(payload.get("prompt") or payload.get("text") or "")
    if event_type == "llm_response":
        return _llm_response_text(payload)
    if event_type == "script_output":
        return str(payload.get("stdout") or "")
    if event_type == "merge_blocked":
        reason = str(payload.get("reason") or "").strip()
        detail = str(payload.get("detail") or "").strip()
        return f"{reason}\n{detail}".strip() if detail else reason
    if event_type == "workflow_paused":
        if _pause_is_silent(payload):
            return ""
        reason = str(payload.get("reason") or "").strip()
        if reason in _PAUSE_REASON_BODIES:
            return _PAUSE_REASON_BODIES[reason]
        return f"paused ({reason})" if reason else "paused"
    return str(payload.get("text") or payload.get("message") or "")


def _compose_message(event: Mapping[str, Any], body: str) -> str:
    """Render the message text for an event.

    Silent narrative events (the quiet running commentary — llm_response,
    agent_output) are sent as their prose alone: the body *is* the message. Loud
    lifecycle events render as their humanized label alone (via _EVENT_LABELS —
    "needs your input", "merge blocked", "done"), optionally followed by the body.
    Identity (id · title · time) is carried once by the always-prepended
    header_line, so no '[title]' bracket is embedded here."""
    body = body.strip() if isinstance(body, str) else ""
    if event.get("type") in _SILENT:
        return body
    etype = event.get("type") or "event"
    label = _EVENT_LABELS.get(etype, etype)
    return f"{label}\n{body}" if body else label


def format_event_message(event: Mapping[str, Any]) -> str:
    """Render one imp event as Telegram message text: for a loud event, the
    humanized label plus the event's human-readable body (resolved per event type);
    for a silent narrative event, the body alone. Identity rides the prepended
    header_line, so no '[title]' bracket is embedded here."""
    payload = event.get("payload") or {}
    return _compose_message(event, event_body(event.get("type"), payload))


def send_message_params(chat_id: int, text: str, *, silent: bool) -> dict[str, Any]:
    """Build a sendMessage payload. silent → disable_notification (the quiet
    running narrative); loud events pass silent=False so the phone buzzes."""
    return {"chat_id": chat_id, "text": text, "disable_notification": silent}


def get_updates_params(offset: int, timeout: int) -> dict[str, Any]:
    """Build a getUpdates payload (long-poll, outbound only)."""
    return {"offset": offset, "timeout": timeout}


def api_call(token: str, method: str, params: Mapping[str, Any]) -> dict[str, Any]:
    """POST one Telegram Bot API call and return the parsed JSON. This is the
    single HTTP seam — tests monkeypatch it (the 'mocked HTTP' of AC 12d)."""
    url = f"{_api_base()}/bot{token}/{method}"
    data = json.dumps(dict(params)).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT) as resp:
        body = resp.read().decode("utf-8")
    result: dict[str, Any] = json.loads(body)
    return result


def make_get_updates(token: str) -> Callable[[int], dict[str, Any]]:
    """Bind a getUpdates caller to the bot token."""
    def get(offset: int) -> dict[str, Any]:
        return api_call(token, "getUpdates", get_updates_params(offset, _LONG_POLL_TIMEOUT))
    return get
