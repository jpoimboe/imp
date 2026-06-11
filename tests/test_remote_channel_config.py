# tests/test_remote_channel_config.py
from __future__ import annotations

from imp.cli.remote_channel import DriverConfig, driver_config_from
from imp.providers.config import Config


def test_driver_config_from_maps_all_fields() -> None:
    cfg = Config(remote={"driver": {
        "command": ["python", "drv.py"],
        "events": ["awaiting_user_input"],
        "startup_timeout": 3.0,
        "reply_timeout": 30.0,
    }})
    dc = driver_config_from(cfg)
    assert dc == DriverConfig(
        command=("python", "drv.py"),
        events=("awaiting_user_input",),
        startup_timeout=3.0,
        reply_timeout=30.0,
    )


def test_driver_config_from_none_when_unconfigured() -> None:
    assert driver_config_from(Config()) is None
    assert driver_config_from(Config(remote={})) is None


def test_driver_config_from_null_reply_timeout() -> None:
    cfg = Config(remote={"driver": {
        "command": ["drv"],
        "events": ["all"],
        "startup_timeout": 10.0,
        "reply_timeout": None,
    }})
    dc = driver_config_from(cfg)
    assert dc is not None
    assert dc.reply_timeout is None
