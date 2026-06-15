from __future__ import annotations

from imp.remote.daemon.commands import ListResult, Notice
from imp.remote.daemon.discover import ChangeInfo
from imp.remote.daemon.focus import Focus
from imp.remote.daemon.telegram_adapter import (
    TelegramNorthAdapter,
    render_response,
)


def _adapter(sent: list) -> TelegramNorthAdapter:
    return TelegramNorthAdapter(
        chat_id=4242, submit=lambda command: None,
        send=lambda text, silent: (sent.append((text, silent)) or 1),
        get_updates=lambda offset: {"result": []}, focus=Focus())


def test_render_listresult_one_line_per_change() -> None:
    out = render_response(ListResult([
        ChangeInfo(id="2026-06-13-k3f9qa", title="Dark mode",
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


def test_render_listresult_multiple_changes_joined_with_newlines() -> None:
    out = render_response(ListResult([
        ChangeInfo(id="2026-06-13-aaaaaa", title="Alpha", status="paused", step="plan"),
        ChangeInfo(id="2026-06-13-bbbbbb", title="Beta", status="running", step="implement"),
    ]))
    assert out == "@aaaaaa  Alpha — paused · plan\n@bbbbbb  Beta — running · implement"


def test_render_listresult_marks_the_focused_change() -> None:
    out = render_response(ListResult([
        ChangeInfo(id="2026-06-13-aaaaaa", title="Alpha", status="paused", step="plan"),
        ChangeInfo(id="2026-06-13-bbbbbb", title="Beta", status="running",
                   step="implement"),
    ]), focused_id="2026-06-13-bbbbbb")
    assert out == (
        "@aaaaaa  Alpha — paused · plan\n"
        "● @bbbbbb  Beta — running · implement")
