from __future__ import annotations

import pytest

from imp.remote.daemon.subscriptions import Subscriptions
from imp.remote.daemon.telegram_adapter import Inbound, TelegramNorthAdapter
from imp.remote.drivers.telegram import ConfigError


def _noop_submit(quest_id: str, text: str) -> None:
    return None


def test_from_env_refuses_without_a_chat_allowlist() -> None:
    with pytest.raises(ConfigError):
        TelegramNorthAdapter.from_env(
            {}, _noop_submit, subscriptions=Subscriptions(),
            is_known_quest=lambda _cid: True)


def test_from_env_refuses_with_a_token_but_no_chat_id() -> None:
    with pytest.raises(ConfigError):
        TelegramNorthAdapter.from_env(
            {"IMP_TELEGRAM_TOKEN": "tok"}, _noop_submit, subscriptions=Subscriptions(),
            is_known_quest=lambda _cid: True)


def test_from_env_builds_an_adapter_when_allowlist_is_present() -> None:
    adapter = TelegramNorthAdapter.from_env(
        {"IMP_TELEGRAM_TOKEN": "tok", "IMP_TELEGRAM_CHAT_ID": "4242"}, _noop_submit,
        subscriptions=Subscriptions(), is_known_quest=lambda _cid: True)
    assert isinstance(adapter, TelegramNorthAdapter)
    assert adapter._chat_id == 4242


def test_from_env_wires_is_known_quest_into_stateless_resolution() -> None:
    adapter = TelegramNorthAdapter.from_env(
        {"IMP_TELEGRAM_TOKEN": "tok", "IMP_TELEGRAM_CHAT_ID": "4242"}, _noop_submit,
        subscriptions=Subscriptions(), is_known_quest=lambda cid: cid == "known1")
    assert adapter.resolve(Inbound(text="@known1 go", reply_to_message_id=None)) == (
        "known1", "go")
    assert adapter.resolve(Inbound(text="@ghost99 go", reply_to_message_id=None)) == (
        None, "@ghost99 go")
