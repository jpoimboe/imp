import pytest

from imp.agents.tools.context import ToolContext
from imp.agents.tools.wait_for_user import AwaitingUserInput, make_wait_for_user_tool


@pytest.fixture
def anyio_backend():
    return "asyncio"


class _Log:
    def __init__(self):
        self.events = []

    def append(self, *, agent_id, type, payload):
        self.events.append({"agent_id": agent_id, "type": type, "payload": payload})
        return {"id": "e1"}


def _ctx(tmp_path, *, user_input_fn, non_interactive, coordinator=None):
    return ToolContext(
        project_root=tmp_path, fs_permissions=(), events_path=tmp_path / "e.jsonl",
        user_input_fn=user_input_fn, non_interactive=non_interactive,
        event_log=_Log(), agent_id="step:review/final-review",
        coordinator=coordinator,
    )


class _FakeCoordinator:
    """Drains a single queued interject, then None — mirrors
    InterruptCoordinator.drain_interject's pop semantics."""

    def __init__(self, queued):
        self._queue = list(queued)

    def drain_interject(self):
        return self._queue.pop(0) if self._queue else None


@pytest.mark.anyio
async def test_attended_returns_reply_and_emits_event(tmp_path):
    ctx = _ctx(tmp_path, user_input_fn=lambda _q: "ship it", non_interactive=False)
    tool = make_wait_for_user_tool()
    result = await tool.async_invoke(ctx, {"prompt": "Merge or rework?"})
    assert result == "ship it"
    evs = [e for e in ctx.event_log.events if e["type"] == "awaiting_user_input"]
    assert evs and evs[0]["agent_id"] == "step:review/final-review"
    assert evs[0]["payload"]["prompt"] == "Merge or rework?"


@pytest.mark.anyio
async def test_headless_raises_awaiting_user_input(tmp_path):
    ctx = _ctx(tmp_path, user_input_fn=None, non_interactive=False)
    tool = make_wait_for_user_tool()
    with pytest.raises(AwaitingUserInput):
        await tool.async_invoke(ctx, {"prompt": "need help"})


@pytest.mark.anyio
async def test_pending_interject_satisfies_wait_for_user(tmp_path):
    # R6: a line typed mid-run was queued as an interject; when the agent then
    # calls wait_for_user, the queued interject SATISFIES the wait — it's returned
    # as the reply and user_input_fn is NOT called (no second solicit).
    def _must_not_solicit(_q):
        raise AssertionError("user_input_fn must not be called when an interject is queued")

    coord = _FakeCoordinator(["use the queued line"])
    ctx = _ctx(tmp_path, user_input_fn=_must_not_solicit, non_interactive=False,
               coordinator=coord)
    tool = make_wait_for_user_tool()
    result = await tool.async_invoke(ctx, {"prompt": "anything pending?"})
    assert result == "use the queued line"


@pytest.mark.anyio
async def test_empty_interject_queue_falls_through_to_solicit(tmp_path):
    # Coordinator present but queue empty → drain returns None → solicit normally.
    coord = _FakeCoordinator([])
    ctx = _ctx(tmp_path, user_input_fn=lambda _q: "solicited", non_interactive=False,
               coordinator=coord)
    tool = make_wait_for_user_tool()
    result = await tool.async_invoke(ctx, {"prompt": "x"})
    assert result == "solicited"


@pytest.mark.anyio
async def test_non_interactive_returns_scripted_reply(tmp_path):
    # New model: --non-interactive is still ATTENDED via the one-shot --message
    # channel. With a user_input_fn that returns a value, the tool RETURNS it
    # regardless of non_interactive (the message-consumed case). The headless
    # pause now comes from the reply fn raising, not from the tool.
    ctx = _ctx(tmp_path, user_input_fn=lambda _q: "scripted", non_interactive=True)
    tool = make_wait_for_user_tool()
    result = await tool.async_invoke(ctx, {"prompt": "x"})
    assert result == "scripted"


@pytest.mark.anyio
async def test_non_interactive_propagates_reply_fn_pause(tmp_path):
    # The message-exhausted case: the one-shot reply fn raises AwaitingUserInput
    # when no --message remains; the tool propagates it to a clean pause.
    def _raise(_q):
        raise AwaitingUserInput()

    ctx = _ctx(tmp_path, user_input_fn=_raise, non_interactive=True)
    tool = make_wait_for_user_tool()
    with pytest.raises(AwaitingUserInput):
        await tool.async_invoke(ctx, {"prompt": "x"})
