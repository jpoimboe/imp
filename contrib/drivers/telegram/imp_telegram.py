"""imp-telegram — reference remote driver for imp (spec Part B).

Reads NDJSON events from imp on stdin, curates each into a Telegram message
(getUpdates is *outbound only* — no inbound port, tunnel, or hosting), and writes
chat replies back to stdout for imp's input-race to pick up. Ships out-of-core
under contrib/; its tests do not gate imp core.

Stdout is reserved for reply lines (one '\\n'-terminated line per reply); all
diagnostics go to stderr.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

# Curation map (spec Decisions §"Default curation"). Curation lives in the
# driver, not in imp: imp only streams event *types*; the driver decides loud /
# silent / dropped.
_LOUD = frozenset({"awaiting_user_input", "errored", "merge_blocked", "completed"})
_SILENT = frozenset({"agent_output", "llm_response"})


def curate(event_type: Any) -> str | None:
    """Classify an imp event type: 'loud' (notify), 'silent' (quiet narrative),
    or None (dropped). Anything not loud/silent is dropped, including tool_call,
    tool_result, thinking, step_context and workflow_paused."""
    if event_type in _LOUD:
        return "loud"
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
    back to an out-of-tree KEY=VALUE file (env wins). The driver REFUSES to start
    without an explicit chat_id allowlist (spec AC 11): only that chat may drive
    the agent. Credentials never come from the repo."""
    file_vals = _parse_kv_file(file_text) if file_text else {}
    token = env.get("IMP_TELEGRAM_TOKEN") or file_vals.get("IMP_TELEGRAM_TOKEN")
    chat_raw = env.get("IMP_TELEGRAM_CHAT_ID") or file_vals.get("IMP_TELEGRAM_CHAT_ID")
    if not chat_raw:
        raise ConfigError(
            "IMP_TELEGRAM_CHAT_ID is required: the driver refuses to start "
            "without an explicit chat allowlist")
    try:
        chat_id = int(chat_raw)
    except ValueError as exc:
        raise ConfigError(
            f"IMP_TELEGRAM_CHAT_ID must be an integer, got {chat_raw!r}") from exc
    if not token:
        raise ConfigError("IMP_TELEGRAM_TOKEN is required")
    return TelegramConfig(token=token, chat_id=chat_id)


def extract_replies(
    updates_response: Mapping[str, Any], chat_id: int, offset: int
) -> tuple[list[str], int]:
    """From a getUpdates response, return (reply_texts, new_offset). Only text
    messages from the allowlisted chat_id become replies (security trust
    boundary: a reply is injected straight into the agent's input). Every update
    advances the offset past itself so the next poll never re-fetches it, which
    is also how only post-startup messages surface (spec AC 10)."""
    replies: list[str] = []
    new_offset = offset
    for upd in updates_response.get("result", []):
        uid = upd.get("update_id")
        if isinstance(uid, int) and uid >= new_offset:
            new_offset = uid + 1
        msg = upd.get("message") or {}
        chat = msg.get("chat") or {}
        if chat.get("id") != chat_id:
            continue
        text = msg.get("text")
        if isinstance(text, str) and text:
            replies.append(text)
    return replies, new_offset


def format_event_message(event: Mapping[str, Any]) -> str:
    """Render one imp event as Telegram message text: a '[title] type' header
    plus any human-readable payload body. Title falls back to the change id, then
    to 'imp'."""
    change = event.get("change") or {}
    title = change.get("title") or change.get("id") or "imp"
    etype = event.get("type") or "event"
    header = f"[{title}] {etype}"
    payload = event.get("payload") or {}
    body = payload.get("text") or payload.get("message") or ""
    if isinstance(body, str) and body.strip():
        return f"{header}\n{body.strip()}"
    return header


def emit_reply(text: str, stream: Any) -> None:
    """Write one driver reply line to stdout. imp reads exactly one line per
    reply (blocking readline + rstrip), so internal CR/LF collapse to spaces
    before the single trailing newline; the stream is flushed immediately."""
    flat = text.replace("\r", " ").replace("\n", " ")
    stream.write(flat + "\n")
    stream.flush()
