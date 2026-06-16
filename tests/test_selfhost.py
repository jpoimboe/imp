from __future__ import annotations

from pathlib import Path

import imp.engine.selfhost as selfhost
from imp.engine.selfhost import SelfHostLaunch, selfhost_launch
from tests.conftest import _make_imp_checkout


def test_non_imp_checkout_has_no_launch(tmp_path: Path) -> None:
    assert selfhost_launch(tmp_path) is None


def test_imp_checkout_returns_shim_and_src_dirs(tmp_path: Path) -> None:
    _make_imp_checkout(tmp_path)
    launch = selfhost_launch(tmp_path)
    assert isinstance(launch, SelfHostLaunch)
    assert launch.shim_dir == str(tmp_path / ".imp" / "selfhost-bin")
    assert launch.src_dir == str(tmp_path / "src")
    shim = tmp_path / ".imp" / "selfhost-bin" / "imp"
    assert shim.exists()
    assert shim.stat().st_mode & 0o111
    body = shim.read_text()
    assert body.startswith("#!/bin/sh\n")
    assert "-m imp" in body


def test_driver_env_priming_helpers_are_gone() -> None:
    assert not hasattr(selfhost, "selfhost_full_env")
    assert not hasattr(selfhost, "selfhost_env_overrides")
