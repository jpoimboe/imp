from __future__ import annotations

from pathlib import Path

from imp.engine.drivers import driver_stdio_servers
from tests.conftest import _make_imp_checkout


def test_no_project_root_keeps_the_plain_stdio_config() -> None:
    assert driver_stdio_servers({"impty"}) == {
        "impty": {"type": "stdio", "command": "python", "args": ["-m", "impty.mcp"]}
    }


def test_non_imp_project_root_keeps_the_plain_stdio_config(tmp_path: Path) -> None:
    cfg = driver_stdio_servers({"impty"}, project_root=tmp_path)
    assert "env" not in cfg["impty"]


def test_imp_checkout_adds_a_worktree_pointing_env(tmp_path: Path) -> None:
    _make_imp_checkout(tmp_path)
    cfg = driver_stdio_servers({"impty"}, project_root=tmp_path)
    env = cfg["impty"]["env"]
    assert env["PYTHONPATH"].split(":")[0] == str(tmp_path / "src")
    assert env["PATH"].split(":")[0] == str(tmp_path / ".imp" / "selfhost-bin")
    assert not any(k.startswith("IMP_") for k in env)
    assert cfg["impty"]["command"] == "python"
    assert cfg["impty"]["args"] == ["-m", "impty.mcp"]
