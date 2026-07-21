from __future__ import annotations

from pathlib import Path

import pytest

from imp.remote.daemon import runner
from imp.remote.session.reply import DeliveryOutcome


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.mark.anyio
async def test_deliver_to_session_loads_the_change_and_injects_the_reply(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    sentinel = object()

    class _FakeQuest:
        @classmethod
        def load(cls, control_root: Path, quest_id: str) -> object:
            assert control_root == tmp_path
            assert quest_id == "2026-06-13-abc"
            return sentinel

    captured: dict[str, object] = {}

    async def _fake_deliver(quest: object, text: str) -> DeliveryOutcome:
        captured["change"] = quest
        captured["text"] = text
        return DeliveryOutcome.NOT_AT_GATE

    monkeypatch.setattr(runner, "Quest", _FakeQuest)
    monkeypatch.setattr(runner, "deliver_session_reply", _fake_deliver)

    outcome = await runner.deliver_to_session(tmp_path, "2026-06-13-abc", "approved")

    assert outcome is DeliveryOutcome.NOT_AT_GATE
    assert captured == {"change": sentinel, "text": "approved"}
