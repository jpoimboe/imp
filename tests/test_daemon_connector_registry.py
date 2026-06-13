from __future__ import annotations

import importlib

from imp.remote.drivers import REGISTRY


def test_daemon_connector_is_registered() -> None:
    entry = REGISTRY["daemon-connector"]
    assert entry.module == "imp.remote.drivers.daemon_connector"
    assert "IMP_REMOTE_CONTROL_SOCK" in entry.required_env


def test_daemon_connector_module_is_importable() -> None:
    mod = importlib.import_module(REGISTRY["daemon-connector"].module)
    assert callable(mod.main)
