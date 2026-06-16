from __future__ import annotations

import os
from pathlib import Path

from imp.engine.selfhost import (
    SelfHostLaunch,
    selfhost_env_overrides,
    selfhost_full_env,
    selfhost_launch,
)

from tests.conftest import _make_imp_checkout


def test_launch_is_none_for_a_non_imp_project(tmp_path: Path) -> None:
    assert selfhost_launch(tmp_path) is None


def test_launch_writes_an_executable_imp_shim_for_a_checkout(tmp_path: Path) -> None:
    _make_imp_checkout(tmp_path)
    launch = selfhost_launch(tmp_path)
    assert launch is not None
    assert launch.src_dir == str(tmp_path / "src")
    shim = Path(launch.shim_dir) / "imp"
    assert os.access(shim, os.X_OK)
    body = shim.read_text()
    assert body.startswith("#!/bin/sh\n")
    assert "-m imp" in body


def test_full_env_strips_outer_imp_vars_and_prefixes_paths() -> None:
    launch = SelfHostLaunch(shim_dir="/wt/.imp/selfhost-bin", src_dir="/wt/src")
    base = {
        "IMP_CHANGE_ID": "outer", "IMP_VERIFY_COMMAND": "make test",
        "HOME": "/home/u", "PATH": "/usr/bin", "PYTHONPATH": "/old/site",
    }
    env = selfhost_full_env(launch, base)
    assert not any(k.startswith("IMP_") for k in env)       # outer engine vars gone
    assert env["HOME"] == "/home/u"                          # ordinary vars kept
    assert env["PATH"] == "/wt/.imp/selfhost-bin:/usr/bin"   # shim dir first
    assert env["PYTHONPATH"] == "/wt/src:/old/site"          # worktree src first


def test_full_env_handles_absent_path_and_pythonpath() -> None:
    launch = SelfHostLaunch(shim_dir="/s", src_dir="/wt/src")
    env = selfhost_full_env(launch, {"HOME": "/h"})
    assert env["PATH"] == "/s"
    assert env["PYTHONPATH"] == "/wt/src"


def test_env_overrides_carries_only_the_two_path_keys() -> None:
    launch = SelfHostLaunch(shim_dir="/s", src_dir="/wt/src")
    ov = selfhost_env_overrides(launch, {"PATH": "/usr/bin", "PYTHONPATH": "/old"})
    assert ov == {"PATH": "/s:/usr/bin", "PYTHONPATH": "/wt/src:/old"}


def test_env_overrides_handles_absent_path_and_pythonpath() -> None:
    launch = SelfHostLaunch(shim_dir="/s", src_dir="/wt/src")
    ov = selfhost_env_overrides(launch, {})
    assert ov == {"PATH": "/s", "PYTHONPATH": "/wt/src"}
