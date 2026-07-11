from __future__ import annotations

from pathlib import Path

import pytest

from imp.remote.daemon import runner


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.mark.anyio
async def test_deliver_to_session_loads_the_change_and_injects_the_reply(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    sentinel = object()

    class _FakeChange:
        @classmethod
        def load(cls, control_root: Path, quest_id: str) -> object:
            assert control_root == tmp_path
            assert quest_id == "2026-06-13-abc"
            return sentinel

    captured: dict[str, object] = {}

    async def _fake_deliver(change: object, text: str) -> None:
        captured["change"] = change
        captured["text"] = text

    monkeypatch.setattr(runner, "Quest", _FakeChange)
    monkeypatch.setattr(runner, "deliver_session_reply", _fake_deliver)

    await runner.deliver_to_session(tmp_path, "2026-06-13-abc", "approved")

    assert captured == {"change": sentinel, "text": "approved"}
