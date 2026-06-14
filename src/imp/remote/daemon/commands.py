"""Transport-agnostic chat-command grammar: inbound text -> Command (spec Decision 3)."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from imp.remote.daemon.discover import ChangeInfo

# resolve_lookup(text, reply_to) -> (change_id | None, stripped_text). Supplied by
# the adapter (its resolve over _msg_to_change / _handle_to_change); pure inputs to parse.
ResolveLookup = Callable[[str, int | None], tuple[str | None, str]]


@dataclass(frozen=True)
class Reply:
    change_id: str
    text: str


@dataclass(frozen=True)
class Create:
    prompt: str


@dataclass(frozen=True)
class Abandon:
    change_id: str


@dataclass(frozen=True)
class ListChanges:
    pass


@dataclass(frozen=True)
class Help:
    pass


@dataclass(frozen=True)
class Notice:
    text: str


@dataclass(frozen=True)
class ListResult:
    changes: list[ChangeInfo]


Command = Reply | Create | Abandon | ListChanges | Help | Notice
Response = ListResult | Notice

NEW_USAGE = "Usage: /new <what to build>"
ABANDON_USAGE = (
    "To abandon a Change, reply to one of its messages or use @handle "
    "(e.g. /abandon @ab12cd).")
UNKNOWN_COMMAND = "Unknown command. Try /help."
ROUTE_HINT = (
    "Couldn't route that — reply to a Change's message, prefix @handle, or send /list.")
HELP_TEXT = (
    "Commands:\n"
    "/list — show changes\n"
    "/new <prompt> — start a Change\n"
    "/abandon (reply or @handle) — stop a Change\n"
    "/help — this message\n"
    "Reply to a Change's message (or @handle) to answer it.")


def _resolve_abandon_target(
    rest: str, reply_to: int | None, resolve_lookup: ResolveLookup,
) -> str | None:
    """Resolve /abandon's target. An @handle needs a trailing token for parse_handle
    to strip it; the command word stands in as that token and is discarded."""
    probe = f"{rest} abandon" if rest else ""
    change_id, _ = resolve_lookup(probe, reply_to)
    return change_id


def parse(text: str, reply_to: int | None, resolve_lookup: ResolveLookup) -> Command:
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
            change_id = _resolve_abandon_target(rest, reply_to, resolve_lookup)
            return Abandon(change_id) if change_id is not None else Notice(ABANDON_USAGE)
        return Notice(UNKNOWN_COMMAND)
    change_id, body = resolve_lookup(text, reply_to)
    return Reply(change_id, body) if change_id is not None else Notice(ROUTE_HINT)
