"""Task 11 AUTHORITY test: reply-vs-interject routing by submit-time state (F7/R6).

Drives the routing CORE (ReplyInterjectRouter) directly — no TTY, no
prompt_toolkit. A line submitted while a solicit is ACTIVE is a reply (delivered
to the reply sink); a line submitted while INACTIVE (agent running) is enqueued
as an interject. Slash interception is scoped to the REPLY path only — it lives
in the line reader (ask_line_with_slash), covered separately against
handle_slash_line. The INTERJECT path does NOT intercept slash: a mid-run `/...`
line is enqueued verbatim and reaches the agent as literal text (a deliberate
scope boundary; see ReplyInterjectRouter's docstring).
"""

from __future__ import annotations

from imp.cli.main import ReplyInterjectRouter


class _FakeCoord:
    def __init__(self) -> None:
        self.interjects: list[str] = []

    def enqueue_interject(self, text: str) -> None:
        self.interjects.append(text)


def test_inactive_line_is_enqueued_as_interject() -> None:
    # No solicit in flight → the agent is running → the line is an INTERJECT.
    coord = _FakeCoord()
    replies: list[str] = []
    router = ReplyInterjectRouter(coord, reply_sink=replies.append)
    assert router.solicit_active is False

    router.route("please also update the README")

    assert coord.interjects == ["please also update the README"]
    assert replies == []        # NOT the reply path


def test_active_line_reaches_reply_path_not_interject() -> None:
    # A solicit is in flight (a prompt is up) → the line is the REPLY: it goes to
    # the reply sink and is NOT enqueued as an interject.
    coord = _FakeCoord()
    replies: list[str] = []
    router = ReplyInterjectRouter(coord, reply_sink=replies.append)

    router.begin_solicit()
    assert router.solicit_active is True
    router.route("ship it")
    router.end_solicit()

    assert replies == ["ship it"]
    assert coord.interjects == []          # reply path — NOT an interject
    assert router.solicit_active is False  # flag cleared after the solicit


def test_route_per_line_by_current_state() -> None:
    # The decision is re-evaluated PER line at submit time: an interject before a
    # solicit, a reply during it, an interject after it ends.
    coord = _FakeCoord()
    replies: list[str] = []
    router = ReplyInterjectRouter(coord, reply_sink=replies.append)

    router.route("before")                 # inactive → interject
    router.begin_solicit()
    router.route("during")                 # active → reply
    router.end_solicit()
    router.route("after")                  # inactive again → interject

    assert coord.interjects == ["before", "after"]
    assert replies == ["during"]


def test_active_line_without_reply_sink_is_dropped_not_interjected() -> None:
    # LIVE wiring: during a solicit the reply comes from the prompt itself, so the
    # router has no reply sink. A (racy) routed line while active must NOT become
    # a stray interject — it is treated as a reply and dropped here.
    coord = _FakeCoord()
    router = ReplyInterjectRouter(coord)   # no reply_sink (the live default)

    router.begin_solicit()
    router.route("noise during prompt")
    router.end_solicit()

    assert coord.interjects == []          # not mis-routed as an interject


# -- slash interception (upstream of the router) ---------------------------
# A slash command is intercepted by ask_line_with_slash BEFORE a line ever
# reaches router.route: /help & /skills print and re-prompt (never returned),
# /<skill> resolves to a skill body. The routing core therefore only ever sees
# already-resolved content. Assert handle_slash_line's interception contract so
# both paths (reply and interject) inherit it.

def test_slash_builtin_is_intercepted_not_routed() -> None:
    from imp.cli.slash import REPROMPT, handle_slash_line

    class _Change:
        class _WF:
            def __truediv__(self, _other: str) -> str:
                return "/nonexistent/skills"
        workflow_dir = _WF()

    printed: list[str] = []
    result = handle_slash_line("/help", change=_Change(), output_fn=printed.append)
    # /help is consumed (re-prompt sentinel) — it never becomes a routable line.
    assert result is REPROMPT
    assert any("slash commands" in line.lower() for line in printed)
