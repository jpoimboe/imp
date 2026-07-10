"""Transport-agnostic chat-command grammar: inbound text -> Command (spec Decision 3)."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from imp.remote.daemon.discover import QuestInfo

# resolve_lookup(text, reply_to) -> (quest_id | None, stripped_text). Supplied by
# the adapter (its resolve over _msg_to_change / _handle_to_change); pure inputs to parse.
ResolveLookup = Callable[[str, int | None], tuple[str | None, str]]


@dataclass(frozen=True)
class Reply:
    quest_id: str
    text: str


@dataclass(frozen=True)
class Create:
    prompt: str


@dataclass(frozen=True)
class Abandon:
    quest_id: str


@dataclass(frozen=True)
class ListChanges:
    pass


@dataclass(frozen=True)
class Help:
    pass


@dataclass(frozen=True)
class Notice:
    text: str
    quest_id: str | None = None


@dataclass(frozen=True)
class Subscribe:
    quest_id: str


@dataclass(frozen=True)
class Unsubscribe:
    quest_id: str


@dataclass(frozen=True)
class Replay:
    quest_id: str | None
    n: int


@dataclass(frozen=True)
class Search:
    query: str


@dataclass(frozen=True)
class Resume:
    token: str


@dataclass(frozen=True)
class Save:
    quest_id: str | None


@dataclass(frozen=True)
class Alerts:
    on: bool | None


@dataclass(frozen=True)
class ListResult:
    changes: list[QuestInfo]
    resident: frozenset[str] = frozenset()


Command = (
    Reply | Create | Abandon | ListChanges | Help | Notice |
    Subscribe | Unsubscribe | Replay | Search | Resume | Save | Alerts)
Response = ListResult | Notice

NEW_USAGE = "Usage: /new <what to build>"
SEARCH_USAGE = "Usage: /search <query>"
RESUME_USAGE = "Usage: /resume <id or search text>"
SAVE_USAGE = (
    "To save a conversation, reply to one of its messages or use @handle "
    "(e.g. /save @ab12cd).")
ABANDON_USAGE = (
    "To abandon a Change, reply to one of its messages or use @handle "
    "(e.g. /abandon @ab12cd).")
UNSUBSCRIBE_USAGE = (
    "To unsubscribe, reply to one of its messages or use @handle "
    "(e.g. /unsubscribe @ab12cd).")
UNKNOWN_COMMAND = "Unknown command. Try /help."
ALERTS_ON_REPLY = "Alerts: all Changes."
ALERTS_OFF_REPLY = "Alerts: subscribed Changes only."
ROUTE_HINT = (
    "Couldn't route that — reply to a Change's message, prefix @handle, or send /list.")
HELP_TEXT = (
    "Commands:\n"
    "/list — show running changes (● = subscribed)\n"
    "/new <prompt> — start a Change\n"
    "/subscribe @handle — follow a Change (streams from now; /replay for history)\n"
    "/unsubscribe (reply or @handle) — stop all messages for a Change\n"
    "/replay [n] (reply or @handle) — re-send last n messages\n"
    "/abandon (reply or @handle) — stop a Change\n"
    "/search <query> — search archived conversations by title or id\n"
    "/resume <id or text> — rehydrate and continue a saved/archived conversation\n"
    "/save (reply or @handle) — save a conversation to your Backlog\n"
    "/alerts on|off — alert on every Change, not just the ones you follow (default: on)\n"
    "/help — this message\n"
    "Reply to a Change's message (or @handle) to answer it.")


def _resolve_target(
    rest: str, reply_to: int | None, resolve_lookup: ResolveLookup, filler: str,
) -> str | None:
    # An @handle needs a trailing token for parse_handle to strip it; filler stands in
    # as that token and is discarded.
    probe = f"{rest} {filler}" if rest else ""
    quest_id, _ = resolve_lookup(probe, reply_to)
    return quest_id


def _parse_replay_n(rest: str) -> int:
    for token in rest.split():
        if token.isdigit():
            return max(1, min(20, int(token)))
    return 5


def parse(
    text: str, reply_to: int | None, resolve_lookup: ResolveLookup,
    resolve_handle: Callable[[str], str | None] | None = None,
) -> Command:
    """Map one inbound message to a Command. A leading '/' (after whitespace strip)
    selects the command path; everything else takes the reply path. Pure given args."""
    stripped = text.strip()
    if stripped.startswith("/"):
        word, _, rest = stripped.partition(" ")
        rest = rest.strip()
        verb = word[1:].lower()
        if verb == "list":
            return ListChanges()
        if verb == "help":
            return Help()
        if verb == "new":
            return Create(rest) if rest else Notice(NEW_USAGE)
        if verb == "abandon":
            quest_id = _resolve_target(rest, reply_to, resolve_lookup, "abandon")
            return Abandon(quest_id) if quest_id is not None else Notice(ABANDON_USAGE)
        if verb in ("subscribe", "sub"):
            quest_id = (
                resolve_handle(rest) if resolve_handle is not None
                else _resolve_target(rest, None, resolve_lookup, "subscribe"))
            return Subscribe(quest_id) if quest_id is not None else Notice(ROUTE_HINT)
        if verb in ("unsubscribe", "unsub", "mute"):
            quest_id = _resolve_target(rest, reply_to, resolve_lookup, "unsubscribe")
            return (
                Unsubscribe(quest_id) if quest_id is not None
                else Notice(UNSUBSCRIBE_USAGE))
        if verb == "replay":
            n = _parse_replay_n(rest)
            quest_id = _resolve_target(rest, reply_to, resolve_lookup, "replay")
            return Replay(quest_id, n)
        if verb == "search":
            return Search(rest) if rest else Notice(SEARCH_USAGE)
        if verb == "resume":
            return Resume(rest) if rest else Notice(RESUME_USAGE)
        if verb == "save":
            return Save(_resolve_target(rest, reply_to, resolve_lookup, "save"))
        if verb == "alerts":
            arg = rest.lower()
            if arg == "on":
                return Alerts(True)
            if arg == "off":
                return Alerts(False)
            return Alerts(None)
        return Notice(UNKNOWN_COMMAND)
    quest_id, body = resolve_lookup(text, reply_to)
    return Reply(quest_id, body) if quest_id is not None else Notice(ROUTE_HINT)
