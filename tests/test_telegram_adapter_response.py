from __future__ import annotations

from imp.remote.daemon.commands import ListResult, Notice
from imp.remote.daemon.discover import ChangeInfo
from imp.remote.daemon.subscriptions import Subscriptions
from imp.remote.daemon.telegram_adapter import TelegramNorthAdapter, render_response


def _adapter(sent: list, subs: Subscriptions | None = None) -> TelegramNorthAdapter:
    return TelegramNorthAdapter(
        chat_id=4242, submit=lambda command: None,
        send=lambda text, silent: (sent.append((text, silent)) or 1),
        get_updates=lambda offset: {"result": []},
        subscriptions=subs if subs is not None else Subscriptions())


def test_render_listresult_one_line_per_change() -> None:
    out = render_response(ListResult([
        ChangeInfo(id="k3f9qa", title="Dark mode",
                   status="running", step="implement")]))
    assert out == "@k3f9qa  Dark mode — running · implement"


def test_render_empty_listresult_says_no_changes() -> None:
    assert render_response(ListResult([])) == "No changes."


def test_render_notice_is_plain_text() -> None:
    assert render_response(Notice("Spawning a new Change…")) == "Spawning a new Change…"


def test_on_response_sends_the_rendered_text_silently() -> None:
    sent: list = []
    _adapter(sent).on_response(Notice("hello"))
    assert sent == [("hello", True)]


def test_on_response_skips_empty_render() -> None:
    sent: list = []
    _adapter(sent).on_response(Notice(""))
    assert sent == []


def test_render_listresult_marks_every_subscribed_change() -> None:
    out = render_response(ListResult([
        ChangeInfo(id="aaaaaa", title="Alpha", status="paused", step="plan"),
        ChangeInfo(id="bbbbbb", title="Beta", status="running", step="implement"),
    ]), ["bbbbbb"])
    assert out == (
        "@aaaaaa  Alpha — paused · plan\n"
        "● @bbbbbb  Beta — running · implement")


def test_on_response_marks_subscribed_rows() -> None:
    subs = Subscriptions()
    subs.add("bbbbbb")
    sent: list = []
    _adapter(sent, subs).on_response(ListResult([
        ChangeInfo(id="bbbbbb", title="Beta", status="running", step="implement")]))
    assert sent == [("● @bbbbbb  Beta — running · implement", True)]
