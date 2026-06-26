from __future__ import annotations

import pytest

from imp.remote.daemon.control import ControlPlane
from imp.remote.daemon.discover import ChangeInfo
from imp.remote.daemon.subscriptions import Subscriptions


async def _noop_deliver(cid: str, text: str) -> None: ...


def _plane(*, delivered: list, created: list, abandoned: list,
           infos: list[ChangeInfo], abandon_rc: int = 0) -> ControlPlane:
    def _abandon(cid: str) -> int:
        abandoned.append(cid)
        return abandon_rc

    async def _deliver(cid: str, text: str) -> None:
        delivered.append((cid, text))

    return ControlPlane(
        deliver=_deliver,
        create=lambda prompt: created.append(prompt),
        abandon=_abandon,
        list_changes=lambda: infos,
        subscriptions=Subscriptions(),
        snapshot=lambda cid: None,
        recent_narrative=lambda cid, n: [])


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.mark.anyio
async def test_reply_delivers_into_the_changes_session() -> None:
    delivered: list = []
    plane = _plane(delivered=delivered, created=[], abandoned=[], infos=[])
    await plane.reply("2026-06-13-abc", "approved")
    assert delivered == [("2026-06-13-abc", "approved")]


def test_create_spawns_a_change_from_the_prompt() -> None:
    created: list = []
    plane = _plane(delivered=[], created=created, abandoned=[], infos=[])
    plane.create("add dark mode")
    assert created == ["add dark mode"]


def test_abandon_targets_the_change_and_returns_exit_code() -> None:
    abandoned: list = []
    plane = _plane(delivered=[], created=[], abandoned=abandoned, infos=[],
                   abandon_rc=2)
    assert plane.abandon("2026-06-13-abc") == 2
    assert abandoned == ["2026-06-13-abc"]


def test_list_returns_the_enumerated_changes() -> None:
    infos = [ChangeInfo(id="a", title="A", status="running", step="plan")]
    plane = _plane(delivered=[], created=[], abandoned=[], infos=infos)
    assert plane.list_changes() == infos


def test_subscriptions_is_exposed_for_the_gate_and_handlers() -> None:
    subs = Subscriptions()
    plane = ControlPlane(
        deliver=_noop_deliver, create=lambda prompt: None, abandon=lambda cid: 0,
        list_changes=lambda: [], subscriptions=subs,
        snapshot=lambda cid: None, recent_narrative=lambda cid, n: [])
    assert plane.subscriptions is subs


def test_full_history_delegates_to_the_wired_callable() -> None:
    seen: list[str] = []
    history = [{"id": "e1", "type": "notice"}]
    plane = ControlPlane(
        deliver=_noop_deliver,
        create=lambda prompt: None,
        abandon=lambda cid: 0,
        list_changes=lambda: [],
        subscriptions=Subscriptions(),
        snapshot=lambda cid: None,
        recent_narrative=lambda cid, n: [],
        full_history=lambda cid: (seen.append(cid) or history))
    assert plane.full_history("alpha") == history
    assert seen == ["alpha"]
