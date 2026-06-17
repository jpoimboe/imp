from __future__ import annotations

import sys

from imp.cli.remote_channel import DriverConfig, driver_config_from
from imp.providers.config import Config


def test_driver_config_from_name_resolves_to_module_argv() -> None:
    cfg = Config(remote={"driver": {"name": "telegram"}})
    dc = driver_config_from(cfg)
    assert dc == DriverConfig(
        command=(sys.executable, "-m", "imp.remote.drivers.telegram"),
        events=("all",),                 # registry default_events
        startup_timeout=10.0,            # built-in default
    )


def test_driver_config_from_applies_knob_overrides() -> None:
    cfg = Config(remote={"driver": {
        "name": "telegram",
        "events": ["errored"],
        "startup_timeout": 3.0,
    }})
    dc = driver_config_from(cfg)
    assert dc is not None
    assert dc.command == (sys.executable, "-m", "imp.remote.drivers.telegram")
    assert dc.events == ("errored",)
    assert dc.startup_timeout == 3.0


def test_driver_config_from_none_when_unconfigured() -> None:
    assert driver_config_from(Config()) is None
    assert driver_config_from(Config(remote={})) is None
