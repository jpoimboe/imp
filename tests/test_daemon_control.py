from __future__ import annotations

import pytest

from imp.remote.daemon.control import ControlPlane
from imp.remote.daemon.discover import ChangeInfo
from imp.remote.daemon.focus import Focus


class FakeHub:
    def __init__(self, *, live: bool) -> None:
        self._live = live
        self.sent: list[tuple[str, str]] = []

    async def send_reply(self, change_id: str, text: str) -> bool:
        if self._live:
            self.sent.append((change_id, text))
            return True
        return False


def _plane(hub: FakeHub, *, resumed: list, created: list, abandoned: list,
           infos: list[ChangeInfo], abandon_rc: int = 0) -> ControlPlane:
    def _abandon(cid: str) -> int:
        abandoned.append(cid)
        return abandon_rc

    return ControlPlane(
        hub=hub,
        resume=lambda cid, text: resumed.append((cid, text)),
        create=lambda prompt: created.append(prompt),
        abandon=_abandon,
        list_changes=lambda: infos,
        focus=Focus(),
        snapshot=lambda cid: None,
        recent_narrative=lambda cid, n: [])


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.mark.anyio
async def test_reply_to_a_live_change_goes_down_the_connector() -> None:
    hub = FakeHub(live=True)
    resumed: list = []
    plane = _plane(hub, resumed=resumed, created=[], abandoned=[], infos=[])
    assert await plane.reply("2026-06-13-abc", "go") == "live"
    assert hub.sent == [("2026-06-13-abc", "go")]
    assert resumed == []   # a live Change is NOT resumed


@pytest.mark.anyio
async def test_reply_to_a_paused_change_falls_back_to_imp_resume() -> None:
    hub = FakeHub(live=False)
    resumed: list = []
    plane = _plane(hub, resumed=resumed, created=[], abandoned=[], infos=[])
    assert await plane.reply("2026-06-13-xyz", "approved") == "resumed"
    assert hub.sent == []                       # nothing went down a connection
    assert resumed == [("2026-06-13-xyz", "approved")]


def test_create_spawns_a_change_from_the_prompt() -> None:
    created: list = []
    plane = _plane(FakeHub(live=False), resumed=[], created=created, abandoned=[], infos=[])
    plane.create("add dark mode")
    assert created == ["add dark mode"]


def test_abandon_targets_the_change_and_returns_exit_code() -> None:
    abandoned: list = []
    plane = _plane(FakeHub(live=False), resumed=[], created=[],
                   abandoned=abandoned, infos=[], abandon_rc=2)
    assert plane.abandon("2026-06-13-abc") == 2
    assert abandoned == ["2026-06-13-abc"]


def test_list_returns_the_enumerated_changes() -> None:
    infos = [ChangeInfo(id="a", title="A", status="running", step="plan")]
    plane = _plane(FakeHub(live=True), resumed=[], created=[], abandoned=[], infos=infos)
    assert plane.list_changes() == infos
