from __future__ import annotations

from imp.remote.daemon.alert_all import AlertAll
from imp.remote.daemon.commands import ListResult, Notice
from imp.remote.daemon.discover import ChangeInfo
from imp.remote.daemon.subscriptions import Subscriptions
from imp.remote.daemon.telegram_adapter import (
    Inbound,
    TelegramNorthAdapter,
    render_response,
)


def _adapter(
    sent: list, subs: Subscriptions | None = None,
    alert_all: AlertAll | None = None,
) -> TelegramNorthAdapter:
    return TelegramNorthAdapter(
        chat_id=4242, submit=lambda command: None,
        send=lambda text, silent: (sent.append((text, silent)) or 1),
        get_updates=lambda offset: {"result": []},
        subscriptions=subs if subs is not None else Subscriptions(),
        alert_all=alert_all)


def test_render_listresult_one_line_per_change() -> None:
    out = render_response(ListResult([
        ChangeInfo(id="k3f9qa", title="Dark mode",
                   status="running", step="implement")],
        frozenset({"k3f9qa"})))
    assert out == "Alerts: subscribed only\n@k3f9qa  Dark mode — running · implement"


def test_render_empty_listresult_says_nothing_running() -> None:
    assert render_response(ListResult([])) == "Alerts: subscribed only\nNothing running."


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
        "Alerts: subscribed only\n"
        "@aaaaaa  Alpha — paused · plan\n"
        "● @bbbbbb  Beta — running · implement")


def test_render_hides_change_neither_resident_nor_subscribed() -> None:
    out = render_response(ListResult([
        ChangeInfo(id="cccccc", title="Gamma", status="paused", step="plan")]))
    assert out == "Alerts: subscribed only\nNothing running."


def test_render_shows_resident_unsubscribed_without_mark() -> None:
    out = render_response(ListResult([
        ChangeInfo(id="dddddd", title="Delta", status="running", step="plan")],
        frozenset({"dddddd"})))
    assert out == "Alerts: subscribed only\n@dddddd  Delta — running · plan"


def test_render_shows_subscribed_not_resident_with_mark() -> None:
    out = render_response(ListResult([
        ChangeInfo(id="eeeeee", title="Epsilon", status="paused", step="plan")]),
        ["eeeeee"])
    assert out == "Alerts: subscribed only\n● @eeeeee  Epsilon — paused · plan"


def test_on_response_marks_subscribed_rows() -> None:
    subs = Subscriptions()
    subs.add("bbbbbb")
    off = AlertAll()
    off.set(False)  # force OFF — default is now on
    sent: list = []
    _adapter(sent, subs, alert_all=off).on_response(ListResult([
        ChangeInfo(id="bbbbbb", title="Beta", status="running", step="implement")]))
    assert sent == [(
        "Alerts: subscribed only\n● @bbbbbb  Beta — running · implement", True)]


def test_on_response_listresult_header_reflects_live_alert_all_mode() -> None:
    alert_all = AlertAll()
    alert_all.set(True)
    sent: list = []
    _adapter(sent, alert_all=alert_all).on_response(ListResult([
        ChangeInfo(id="bbbbbb", title="Beta", status="running", step="implement")],
        frozenset({"bbbbbb"})))
    assert sent == [(
        "Alerts: all changes\n@bbbbbb  Beta — running · implement", True)]


def test_render_listresult_header_reflects_alerts_on() -> None:
    out = render_response(ListResult([
        ChangeInfo(id="k3f9qa", title="Dark mode", status="running", step="implement")],
        frozenset({"k3f9qa"})), (), True)
    assert out == "Alerts: all changes\n@k3f9qa  Dark mode — running · implement"


def test_render_listresult_header_reflects_alerts_off() -> None:
    out = render_response(ListResult([
        ChangeInfo(id="k3f9qa", title="Dark mode", status="running", step="implement")],
        frozenset({"k3f9qa"})), (), False)
    assert out == "Alerts: subscribed only\n@k3f9qa  Dark mode — running · implement"


def test_on_response_notice_with_change_id_is_reply_routable() -> None:
    sent: list = []
    adapter = _adapter(sent)
    adapter.on_response(Notice("replayed body", change_id="k3f9qa"))
    # Still sent silently — a catch-up must not buzz.
    assert sent == [("replayed body", True)]
    # The recorded message id (1, from the fake send) routes a reply to the Change,
    assert adapter.resolve(
        Inbound(text="ok", reply_to_message_id=1)) == ("k3f9qa", "ok")
    # and so does an @handle prefix.
    assert adapter.resolve(
        Inbound(text="@k3f9qa go", reply_to_message_id=None)) == ("k3f9qa", "go")


def test_on_response_plain_notice_records_no_route() -> None:
    sent: list = []
    adapter = _adapter(sent)
    adapter.on_response(Notice("hello"))
    # A Notice with no change_id records nothing — an unrelated reply stays unrouted.
    assert adapter.resolve(
        Inbound(text="ok", reply_to_message_id=1)) == (None, "ok")
