from __future__ import annotations

import dataclasses

import pytest

from imp.remote.drivers import REGISTRY, BuiltinDriver


def test_builtin_driver_is_frozen() -> None:
    d = BuiltinDriver(
        name="x", module="m", description="d",
        required_env=("E",), default_events=("all",))
    assert dataclasses.is_dataclass(d)
    with pytest.raises(dataclasses.FrozenInstanceError):
        d.name = "y"  # type: ignore[misc]


def test_telegram_entry_matches_spec_contract() -> None:
    entry = REGISTRY["telegram"]
    assert entry.module == "imp.remote.drivers.telegram"
    assert entry.required_env == ("IMP_TELEGRAM_TOKEN", "IMP_TELEGRAM_CHAT_ID")
    assert entry.default_events == ("all",)
    assert entry.description.strip()                 # a non-empty one-liner
    # The registry key is the canonical driver name and the module's leaf.
    assert REGISTRY["telegram"].name == "telegram"
    assert entry.module.endswith(".telegram")
