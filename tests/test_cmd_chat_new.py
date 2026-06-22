from __future__ import annotations

from argparse import Namespace

import imp.cli.main as m


def test_cmd_chat_uses_chat_leave_mode(monkeypatch) -> None:
    captured: dict[str, object] = {}

    async def _fake(args: Namespace, *, leave_mode: str = "new") -> int:
        captured["leave_mode"] = leave_mode
        return 0

    monkeypatch.setattr(m, "_async_cmd_new", _fake)
    rc = m._cmd_chat(Namespace())
    assert rc == 0
    assert captured["leave_mode"] == "chat"


def test_cmd_new_uses_new_leave_mode(monkeypatch) -> None:
    captured: dict[str, object] = {}

    async def _fake(args: Namespace, *, leave_mode: str = "UNSET") -> int:
        captured["leave_mode"] = leave_mode
        return 0

    monkeypatch.setattr(m, "_async_cmd_new", _fake)
    rc = m._cmd_new(Namespace())
    assert rc == 0
    assert captured["leave_mode"] == "new"
