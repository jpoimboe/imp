# tests/test_tui_input.py
from __future__ import annotations

import asyncio

import pytest

from imp.cli import narrative as nv
from imp.cli.prompt import ESC_SENTINEL
from imp.cli.tui import NarrativeApp


def _tree():
    return nv.build_tree([{"id": "1", "agent_id": None, "type": "change_created",
                           "payload": {"change_id": 1, "title": "t"}}])


def test_submitting_a_line_resolves_the_request_future() -> None:
    async def go():
        app = NarrativeApp(_tree(), live=True, mouse=False)
        fut = asyncio.ensure_future(app.request_line("> "))
        await asyncio.sleep(0)              # let request_line install the future
        app._submit_input("hello there")
        assert await fut == "hello there"
        assert app._input_buffer_text() == ""   # buffer cleared after submit
    asyncio.run(go())


def test_escape_resolves_future_with_esc_sentinel() -> None:
    async def go():
        app = NarrativeApp(_tree(), live=True, mouse=False)
        fut = asyncio.ensure_future(app.request_line("> "))
        await asyncio.sleep(0)
        app._escape_input()
        assert await fut == ESC_SENTINEL
    asyncio.run(go())


def test_user_chat_input_fn_takes_a_question_and_returns_awaitable() -> None:
    async def go():
        app = NarrativeApp(_tree(), live=True, mouse=False)
        awaitable = app.user_chat_input_fn("anything?")
        fut = asyncio.ensure_future(awaitable)
        await asyncio.sleep(0)
        app._submit_input("yes")
        assert await fut == "yes"
    asyncio.run(go())


def test_gate_option_request_resolves_with_selected_label() -> None:
    import asyncio
    async def go():
        app = NarrativeApp(_tree(), live=True, mouse=False)
        fut = asyncio.ensure_future(app.request_gate_choice(
            "Merge?", [{"label": "Merge"}, {"label": "Back to implement"}]))
        await asyncio.sleep(0)
        assert app._gate_active is True
        app._select_gate_option(1)          # picks "Back to implement"
        assert await fut == "Back to implement"
        assert app._gate_active is False
    asyncio.run(go())


def test_human_gate_dispatch_routes_input_fn_to_selector() -> None:
    # Goes through human_input_fn (what run_change actually calls), NOT
    # request_gate_choice directly — this is the path the wiring gap broke.
    import asyncio
    async def go():
        app = NarrativeApp(_tree(), live=True, mouse=False)
        # Engine emits human_gate_pending, then calls human_input_fn:
        app.on_event({"id": "g", "agent_id": None, "type": "human_gate_pending",
                      "payload": {"phase": "review", "prompt": "Merge?",
                                  "options": [{"label": "Merge"}, {"label": "Back"}]}})
        fut = asyncio.ensure_future(app.human_input_fn())
        await asyncio.sleep(0)
        assert app._gate_active is True          # routed to the selector, not a bare line
        app._select_gate_option(0)
        assert await fut == "Merge"
    asyncio.run(go())


def test_human_output_fn_surfaces_validation_feedback() -> None:
    app = NarrativeApp(_tree(), live=True, mouse=False)
    app.human_output_fn("─── HUMAN GATE ───")     # decorative → ignored
    assert app.status_note == ""
    app.human_output_fn("  (no match for 'xyz'; try again)")
    assert "no match" in app.status_note          # validation feedback surfaced


def test_escape_key_during_gate_resolves_with_esc_sentinel_for_reprompt() -> None:
    """ESC key during an active gate should resolve _gate_future with ESC_SENTINEL
    (re-prompt semantics), not cancel it — _match_human_option sees no match and
    run_human_gate loops back to ask again."""
    async def go():
        app = NarrativeApp(_tree(), live=True, mouse=False)
        fut = asyncio.ensure_future(app.request_gate_choice(
            "Merge?", [{"label": "Merge"}, {"label": "Back"}]))
        await asyncio.sleep(0)
        assert app._gate_active is True
        # ESC key path: cancel_gate defaults to False → ESC_SENTINEL
        app._escape_input()
        assert fut.done()
        assert await fut == ESC_SENTINEL          # re-prompt path, not a cancel
        assert app._gate_future is None
        assert app._gate_active is False
        assert app._gate_options == []
    asyncio.run(go())


def test_request_pause_during_gate_cancels_gate_future_no_hang() -> None:
    """Pressing Ctrl-C while a human gate is pending must cancel the gate future
    so run_human_gate's `await result` raises CancelledError and the engine
    unwinds — the process must NOT hang waiting for a gate that will never be
    resolved."""
    async def go():
        app = NarrativeApp(_tree(), live=True, mouse=False)
        fut = asyncio.ensure_future(app.request_gate_choice(
            "Merge?", [{"label": "Merge"}, {"label": "Back"}]))
        await asyncio.sleep(0)
        assert app._gate_active is True
        app.request_pause()
        # Gate future must be done (cancelled) so no awaiter can hang.
        assert fut.done(), "gate future still pending after request_pause — would hang"
        assert fut.cancelled()                    # CancelledError unwinds run_human_gate
        assert app._gate_future is None
        assert app._gate_active is False
        assert app._gate_options == []
    asyncio.run(go())


def test_request_exit_eof_during_gate_cancels_gate_future_no_hang() -> None:
    """Ctrl-D (EOF) while a human gate is pending must cancel the gate future
    so the engine unwinds rather than hanging."""
    async def go():
        app = NarrativeApp(_tree(), live=True, mouse=False)
        fut = asyncio.ensure_future(app.request_gate_choice(
            "Merge?", [{"label": "Merge"}, {"label": "Back"}]))
        await asyncio.sleep(0)
        assert app._gate_active is True
        app.request_exit_eof()
        assert fut.done(), "gate future still pending after request_exit_eof — would hang"
        assert fut.cancelled()
        assert app._gate_future is None
        assert app._gate_active is False
    asyncio.run(go())
