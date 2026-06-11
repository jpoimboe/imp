"""imp-telegram — reference remote driver for imp (spec Part B).

Reads NDJSON events from imp on stdin, curates each into a Telegram message
(getUpdates is *outbound only* — no inbound port, tunnel, or hosting), and writes
chat replies back to stdout for imp's input-race to pick up. Ships out-of-core
under contrib/; its tests do not gate imp core.

Stdout is reserved for reply lines (one '\\n'-terminated line per reply); all
diagnostics go to stderr.
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# Curation map (spec Decisions §"Default curation"). Curation lives in the
# driver, not in imp: imp only streams event *types*; the driver decides loud /
# silent / dropped.
_LOUD = frozenset({"awaiting_user_input", "errored", "merge_blocked", "completed"})
_SILENT = frozenset({"agent_output", "llm_response"})

_API_BASE = "https://api.telegram.org"
_LONG_POLL_TIMEOUT = 25   # seconds; getUpdates long-poll window
_HTTP_TIMEOUT = 35        # seconds; urlopen ceiling, must exceed the long-poll
_POLL_GAP = 1.0   # seconds to back off after a failed poll cycle


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
    url = f"{_API_BASE}/bot{token}/{method}"
    data = json.dumps(dict(params)).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT) as resp:
        body = resp.read().decode("utf-8")
    result: dict[str, Any] = json.loads(body)
    return result


def make_send(token: str) -> Callable[..., None]:
    """Bind a sendMessage caller to the bot token."""
    def send(chat_id: int, text: str, *, silent: bool) -> None:
        api_call(token, "sendMessage", send_message_params(chat_id, text, silent=silent))
    return send


def make_get_updates(token: str) -> Callable[[int], dict[str, Any]]:
    """Bind a getUpdates caller to the bot token."""
    def get(offset: int) -> dict[str, Any]:
        return api_call(token, "getUpdates", get_updates_params(offset, _LONG_POLL_TIMEOUT))
    return get


def handle_event(
    event: Mapping[str, Any],
    send: Callable[..., None],
    chat_id: int,
) -> bool:
    """Curate one event and, if loud/silent, send it to the chat. Returns True
    iff a message was sent (loud → notify, silent → disable_notification).
    Dropped events return False without contacting Telegram."""
    kind = curate(event.get("type"))
    if kind is None:
        return False
    send(chat_id, format_event_message(event), silent=(kind == "silent"))
    return True


def poll_once(
    get_updates: Callable[[int], Mapping[str, Any]],
    chat_id: int,
    offset: int,
    write_reply: Callable[[str], None],
) -> int:
    """Run one getUpdates cycle: fetch from `offset`, write each allowlisted
    reply via write_reply, and return the advanced offset for the next cycle."""
    replies, new_offset = extract_replies(get_updates(offset), chat_id, offset)
    for reply in replies:
        write_reply(reply)
    return new_offset


def _log(msg: str) -> None:
    """Diagnostics go to stderr; stdout is reserved for reply lines."""
    print(f"imp-telegram: {msg}", file=sys.stderr, flush=True)


def _event_loop(stdin: Any, send: Callable[..., None], chat_id: int) -> None:
    """Read NDJSON events from imp on stdin and relay curated ones to Telegram.
    A send failure is logged and swallowed — the driver must never die on a
    transient API error (and must never block imp)."""
    for raw in stdin:
        line = raw.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        try:
            handle_event(event, send, chat_id)
        except Exception as exc:  # best-effort: never die on a transient API error
            _log(f"send failed: {exc}")


def _poll_loop(
    get_updates: Callable[[int], Mapping[str, Any]],
    chat_id: int,
    offset: int,
    write_reply: Callable[[str], None],
) -> None:
    """Long-poll getUpdates forever, writing allowlisted replies to stdout. A
    poll error backs off briefly and retries; the thread is a daemon, so it dies
    with the process when imp tears the driver down."""
    while True:
        try:
            offset = poll_once(get_updates, chat_id, offset, write_reply)
        except Exception as exc:  # best-effort: back off and retry, never crash
            _log(f"poll failed: {exc}")
            time.sleep(_POLL_GAP)


def main(
    env: Mapping[str, str] | None = None,
    stdin: Any = None,
    stdout: Any = None,
) -> int:
    """Entry point. Loads config (refusing without the chat_id allowlist),
    drains pre-startup backlog, starts the getUpdates poll thread, then relays
    stdin events until EOF (imp tore us down). Returns 2 on a config refusal."""
    env = os.environ if env is None else env
    stdin = sys.stdin if stdin is None else stdin
    stdout = sys.stdout if stdout is None else stdout

    file_text: str | None = None
    cfg_path = env.get("IMP_TELEGRAM_CONFIG_FILE")
    if cfg_path and Path(cfg_path).is_file():
        file_text = Path(cfg_path).read_text(encoding="utf-8")

    try:
        cfg = load_config(env, file_text)
    except ConfigError as exc:
        _log(str(exc))
        return 2

    send = make_send(cfg.token)
    get_updates = make_get_updates(cfg.token)

    # Skip the pre-startup backlog: drain once, keep only the advanced offset so
    # only post-startup messages surface (spec AC 10).
    try:
        _, offset = extract_replies(get_updates(0), cfg.chat_id, 0)
    except Exception as exc:  # best-effort: a failed drain just starts at offset 0
        _log(f"initial getUpdates failed: {exc}")
        offset = 0

    def write_reply(text: str) -> None:
        emit_reply(text, stdout)

    poll_thread = threading.Thread(
        target=_poll_loop,
        args=(get_updates, cfg.chat_id, offset, write_reply),
        daemon=True,
    )
    poll_thread.start()

    _event_loop(stdin, send, cfg.chat_id)
    return 0


if __name__ == "__main__":
    sys.exit(main())
