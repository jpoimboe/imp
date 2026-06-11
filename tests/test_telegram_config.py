from __future__ import annotations

import pytest

from imp.remote.drivers import telegram as imp_telegram


def test_load_config_from_env() -> None:
    cfg = imp_telegram.load_config(
        {"IMP_TELEGRAM_TOKEN": "tok", "IMP_TELEGRAM_CHAT_ID": "42"})
    assert cfg == imp_telegram.TelegramConfig(token="tok", chat_id=42)


def test_load_config_falls_back_to_file() -> None:
    cfg = imp_telegram.load_config(
        {},
        file_text="IMP_TELEGRAM_TOKEN=ftok\nIMP_TELEGRAM_CHAT_ID=7\n",
    )
    assert cfg == imp_telegram.TelegramConfig(token="ftok", chat_id=7)


def test_env_takes_precedence_over_file() -> None:
    cfg = imp_telegram.load_config(
        {"IMP_TELEGRAM_TOKEN": "envtok", "IMP_TELEGRAM_CHAT_ID": "1"},
        file_text="IMP_TELEGRAM_TOKEN=ftok\nIMP_TELEGRAM_CHAT_ID=99\n",
    )
    assert cfg == imp_telegram.TelegramConfig(token="envtok", chat_id=1)


def test_refuses_without_chat_id_allowlist() -> None:
    # Spec AC 11: the driver must refuse to start without the chat_id allowlist.
    with pytest.raises(imp_telegram.ConfigError):
        imp_telegram.load_config({"IMP_TELEGRAM_TOKEN": "tok"})


def test_refuses_non_integer_chat_id() -> None:
    with pytest.raises(imp_telegram.ConfigError):
        imp_telegram.load_config(
            {"IMP_TELEGRAM_TOKEN": "tok", "IMP_TELEGRAM_CHAT_ID": "not-a-number"})


def test_refuses_without_token() -> None:
    with pytest.raises(imp_telegram.ConfigError):
        imp_telegram.load_config({"IMP_TELEGRAM_CHAT_ID": "42"})
