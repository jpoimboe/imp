from __future__ import annotations

import pytest

from imp.remote.daemon.subscriptions import Subscriptions
from imp.remote.daemon.telegram_adapter import TelegramNorthAdapter
from imp.remote.drivers.telegram import ConfigError


def _noop_submit(change_id: str, text: str) -> None:
    return None


def test_from_env_refuses_without_a_chat_allowlist() -> None:
    with pytest.raises(ConfigError):
        TelegramNorthAdapter.from_env({}, _noop_submit, subscriptions=Subscriptions())


def test_from_env_refuses_with_a_token_but_no_chat_id() -> None:
    with pytest.raises(ConfigError):
        TelegramNorthAdapter.from_env(
            {"IMP_TELEGRAM_TOKEN": "tok"}, _noop_submit, subscriptions=Subscriptions())


def test_from_env_builds_an_adapter_when_allowlist_is_present() -> None:
    adapter = TelegramNorthAdapter.from_env(
        {"IMP_TELEGRAM_TOKEN": "tok", "IMP_TELEGRAM_CHAT_ID": "4242"}, _noop_submit,
        subscriptions=Subscriptions())
    assert isinstance(adapter, TelegramNorthAdapter)
    assert adapter._chat_id == 4242
