import pytest

from imp.agents.tools.wait_for_user import AwaitingUserInput
from imp.cli.main import make_message_reply_fn


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.mark.anyio
async def test_message_reply_returns_message_then_pauses() -> None:
    reply = make_message_reply_fn("ship it")
    assert await reply() == "ship it"          # first solicit consumes --message
    with pytest.raises(AwaitingUserInput):     # second solicit: none left → pause
        await reply()


@pytest.mark.anyio
async def test_message_reply_none_pauses_immediately() -> None:
    reply = make_message_reply_fn(None)
    with pytest.raises(AwaitingUserInput):     # no --message → pause on first solicit
        await reply()
