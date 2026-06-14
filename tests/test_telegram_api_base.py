from __future__ import annotations

import urllib.request
from typing import Any

import pytest

from imp.remote.drivers.telegram import _api_base, api_call


def test_api_base_defaults_to_telegram(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("IMP_TELEGRAM_API_BASE", raising=False)
    assert _api_base() == "https://api.telegram.org"


def test_api_base_reads_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("IMP_TELEGRAM_API_BASE", "http://127.0.0.1:8081")
    assert _api_base() == "http://127.0.0.1:8081"


def test_api_call_targets_the_env_base(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("IMP_TELEGRAM_API_BASE", "http://127.0.0.1:8081")
    seen: dict[str, Any] = {}

    class _FakeResp:
        def __enter__(self) -> _FakeResp:
            return self

        def __exit__(self, *a: Any) -> bool:
            return False

        def read(self) -> bytes:
            return b'{"ok": true}'

    def fake_urlopen(req: Any, timeout: float = 0) -> _FakeResp:
        seen["url"] = req.full_url
        return _FakeResp()

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    api_call("TOK", "getMe", {})
    assert seen["url"] == "http://127.0.0.1:8081/botTOK/getMe"
