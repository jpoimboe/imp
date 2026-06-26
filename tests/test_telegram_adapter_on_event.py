from __future__ import annotations

from imp.remote.daemon.focus import Focus
from imp.remote.daemon.telegram_adapter import Inbound, TelegramNorthAdapter

CHANGE_ID = "aabbcc"
OTHER_CHANGE_ID = "zzxxww"

LOUD_EVENT = {
    "type": "awaiting_user_input",
    "payload": {"prompt": "Need your call"},
    "change": {"id": "k3f9qa", "title": "Dark mode"},
}

SILENT_EVENT = {
    "type": "agent_output",
    "payload": {"text": "working on it"},
    "change": {"id": CHANGE_ID, "title": "My change"},
}


def _adapter(
    sent: list, *, message_id: int | None = 42, focus: Focus | None = None
) -> TelegramNorthAdapter:
    def send(text: str, silent: bool) -> int | None:
        sent.append((text, silent))
        return message_id

    return TelegramNorthAdapter(
        chat_id=4242, submit=lambda _: None,
        send=send, get_updates=lambda _: {"result": []},
        focus=focus if focus is not None else Focus())


def _make_adapter(focus: Focus) -> tuple[TelegramNorthAdapter, list[tuple[str, bool]]]:
    sent: list[tuple[str, bool]] = []

    def fake_send(text: str, silent: bool) -> int | None:
        sent.append((text, silent))
        return 42

    adapter = TelegramNorthAdapter(
        chat_id=1234,
        submit=lambda _: None,
        send=fake_send,
        get_updates=lambda _: {},
        focus=focus,
    )
    return adapter, sent


def test_on_event_sends_a_message_carrying_the_handle() -> None:
    sent: list = []
    adapter = _adapter(sent)
    adapter.on_event(LOUD_EVENT)
    assert len(sent) == 1
    text, silent = sent[0]
    assert "k3f9qa" in text and "Need your call" in text
    assert silent is False


def test_on_event_records_reply_to_mapping_so_a_reply_resolves_home() -> None:
    adapter = _adapter([])
    adapter.on_event(LOUD_EVENT)
    reply = Inbound(text="yes do it", reply_to_message_id=42)
    assert adapter.resolve(reply) == ("k3f9qa", "yes do it")


def test_on_event_records_handle_mapping_for_the_at_fallback() -> None:
    adapter = _adapter([])
    adapter.on_event(LOUD_EVENT)
    reply = Inbound(text="@k3f9qa go", reply_to_message_id=None)
    assert adapter.resolve(reply) == ("k3f9qa", "go")


def test_on_event_drops_a_silent_event_with_an_empty_body() -> None:
    sent: list = []
    focus = Focus()
    focus.focus_on("k3f9qa")
    adapter = _adapter(sent, focus=focus)
    adapter.on_event({"type": "llm_response", "payload": {},
                      "change": {"id": "k3f9qa", "title": "X"}})
    assert sent == []


def test_on_event_records_handle_map_even_when_send_returns_none() -> None:
    adapter = _adapter([], message_id=None)
    adapter.on_event(LOUD_EVENT)
    reply = Inbound(text="@k3f9qa go", reply_to_message_id=None)
    assert adapter.resolve(reply) == ("k3f9qa", "go")


def test_silent_unfocused_event_populates_handle_map_without_sending() -> None:
    """A silent event for a change that isn't focused should still register the
    handle→change_id mapping so the user can address it with @handle later."""
    focus = Focus()
    focus.focus_on(OTHER_CHANGE_ID)
    adapter, sent = _make_adapter(focus)

    adapter.on_event(SILENT_EVENT)

    assert adapter._handle_to_change.get("aabbcc") == CHANGE_ID
    assert sent == []
    assert adapter._msg_to_change == {}


def _narration(text: str) -> dict:
    return {"type": "llm_response",
            "change": {"id": "k3f9qa", "title": "t"},
            "payload": {"raw": {"content": [{"type": "text", "text": text}]}}}


def _prompt_less_await() -> dict:
    return {"type": "awaiting_user_input",
            "change": {"id": "k3f9qa", "title": "t"},
            "payload": {"prompt": ""}}


def test_prompt_less_await_falls_back_to_preceding_narration() -> None:
    sent: list = []
    adapter = _adapter(sent)
    adapter.on_event(_narration("Should I merge the branch?"))
    adapter.on_event(_prompt_less_await())
    loud = [text for text, silent in sent if not silent]
    assert any("Should I merge the branch?" in text for text in loud)


def test_prompt_less_await_with_no_narration_sends_header_only() -> None:
    sent: list = []
    adapter = _adapter(sent)
    adapter.on_event(_prompt_less_await())
    loud = [text for text, silent in sent if not silent]
    assert len(loud) == 1


def test_consecutive_prompt_less_awaits_reuse_last_narration() -> None:
    sent: list = []
    adapter = _adapter(sent)
    adapter.on_event(_narration("Proceed with the merge?"))
    adapter.on_event(_prompt_less_await())
    adapter.on_event(_prompt_less_await())
    loud = [text for text, silent in sent if not silent]
    assert len(loud) == 2
    assert all("Proceed with the merge?" in text for text in loud)


def _step_started(
    change_id: str, *, phase: str | None = None, label: str = "scoping",
    quiet: bool = False,
) -> dict:
    payload: dict = {"label": label}
    if phase is not None:
        payload["phase_label"] = phase
    if quiet:
        payload["quiet"] = True
    return {"type": "step_started",
            "change": {"id": change_id, "title": "t"}, "payload": payload}


def test_step_started_renders_silent_header_less_divider_for_focused_change() -> None:
    focus = Focus()
    focus.focus_on("k3f9qa")
    sent: list = []
    _adapter(sent, focus=focus).on_event(
        _step_started("k3f9qa", phase="Brainstorm", label="scoping"))
    assert sent == [("— Brainstorm › scoping —", True)]


def test_step_started_divider_dropped_for_unfocused_change() -> None:
    focus = Focus()
    focus.focus_on("other")
    sent: list = []
    _adapter(sent, focus=focus).on_event(_step_started("k3f9qa"))
    assert sent == []


def test_quiet_step_started_renders_no_divider() -> None:
    focus = Focus()
    focus.focus_on("k3f9qa")
    sent: list = []
    _adapter(sent, focus=focus).on_event(_step_started("k3f9qa", quiet=True))
    assert sent == []
