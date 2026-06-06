import os
import re
from pathlib import Path

from imp.providers.codex_config import provision_codex_home


def test_provision_codex_home_modes_and_pid_dirname(tmp_path: Path) -> None:
    home = provision_codex_home(
        tmp_root=tmp_path, host="127.0.0.1", port=49152,
        bearer_env_var="IMP_MCP_TOKEN", web_search_enabled=False,
    )
    # Directory under tmp_root, mode 0700.
    assert home.parent == tmp_path
    assert (home.stat().st_mode & 0o777) == 0o700
    # Encodes the owner pid so the sweep can skip live-owner dirs (L4).
    m = re.match(r"imp-codex-(\d+)-", home.name)
    assert m is not None
    assert int(m.group(1)) == os.getpid()
    # config.toml is 0600 and contains the expected sections.
    cfg = home / "config.toml"
    assert cfg.exists()
    assert (cfg.stat().st_mode & 0o777) == 0o600
    txt = cfg.read_text()
    assert "[mcp_servers.imp]" in txt
    assert "[features]" in txt
    # web_search disabled by default (no declared web tool).
    assert 'web_search = "disabled"' in txt


def test_provision_codex_home_dirs_are_unique(tmp_path: Path) -> None:
    a = provision_codex_home(tmp_root=tmp_path, host="127.0.0.1", port=1,
                             bearer_env_var="X", web_search_enabled=False)
    b = provision_codex_home(tmp_root=tmp_path, host="127.0.0.1", port=1,
                             bearer_env_var="X", web_search_enabled=False)
    assert a != b
