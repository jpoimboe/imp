from __future__ import annotations

from imp.remote.daemon.commands import CatchupAlert, ListResult, Notice
from imp.remote.daemon.discover import ChangeInfo
from imp.remote.daemon.subscriptions import Subscriptions
from imp.remote.daemon.telegram_adapter import Inbound, TelegramNorthAdapter, render_response


def _adapter(sent: list, subs: Subscriptions | None = None) -> TelegramNorthAdapter:
    return TelegramNorthAdapter(
        chat_id=4242, submit=lambda command: None,
        send=lambda text, silent: (sent.append((text, silent)) or 1),
        get_updates=lambda offset: {"result": []},
        subscriptions=subs if subs is not None else Subscriptions())


def test_render_listresult_one_line_per_change() -> None:
    out = render_response(ListResult([
        ChangeInfo(id="k3f9qa", title="Dark mode",
                   status="running", step="implement")],
        frozenset({"k3f9qa"})))
    assert out == "@k3f9qa  Dark mode — running · implement"


def test_render_empty_listresult_says_nothing_running() -> None:
    assert render_response(ListResult([])) == "Nothing running."


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
    ], frozenset({"aaaaaa", "bbbbbb"})), ["bbbbbb"])
    assert out == (
        "@aaaaaa  Alpha — paused · plan\n"
        "● @bbbbbb  Beta — running · implement")


def test_render_hides_change_neither_resident_nor_subscribed() -> None:
    out = render_response(ListResult([
        ChangeInfo(id="cccccc", title="Gamma", status="paused", step="plan")]))
    assert out == "Nothing running."


def test_render_shows_resident_unsubscribed_without_mark() -> None:
    out = render_response(ListResult([
        ChangeInfo(id="dddddd", title="Delta", status="running", step="plan")],
        frozenset({"dddddd"})))
    assert out == "@dddddd  Delta — running · plan"


def test_render_shows_subscribed_not_resident_with_mark() -> None:
    out = render_response(ListResult([
        ChangeInfo(id="eeeeee", title="Epsilon", status="paused", step="plan")]),
        ["eeeeee"])
    assert out == "● @eeeeee  Epsilon — paused · plan"


def test_on_response_marks_subscribed_rows() -> None:
    subs = Subscriptions()
    subs.add("bbbbbb")
    sent: list = []
    _adapter(sent, subs).on_response(ListResult([
        ChangeInfo(id="bbbbbb", title="Beta", status="running", step="implement")]))
    assert sent == [("● @bbbbbb  Beta — running · implement", True)]


def test_render_catchup_is_one_line_per_change() -> None:
    out = render_response(CatchupAlert([
        ChangeInfo(id="aaaaaa", title="Alpha", status="paused", step="plan"),
        ChangeInfo(id="cccccc", title="Gamma", status="merge_blocked", step="finalize"),
    ]))
    assert out == (
        "@aaaaaa  Alpha — paused\n"
        "@cccccc  Gamma — merge_blocked")


def test_on_response_catchup_records_handle_map_for_reply_routing() -> None:
    sent: list = []
    adapter = _adapter(sent)
    adapter.on_response(CatchupAlert([
        ChangeInfo(id="k3f9qa", title="Dark mode", status="paused", step="plan")]))
    assert sent == [("@k3f9qa  Dark mode — paused", True)]
    assert adapter.resolve(Inbound(text="@k3f9qa go", reply_to_message_id=None)) == (
        "k3f9qa", "go")
