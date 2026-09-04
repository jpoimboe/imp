"""Guard: codex still accepts the config key that keeps imp's MCP tools working.

The session runs under codex's `read-only` sandbox, where every imp MCP call is
auto-approved solely by `default_tools_approval_mode` in each `[mcp_servers.*]` block.
imp does not pass `--strict-config` at runtime and codex silently ignores unknown config
keys, so an upstream rename of that key would break every tool call with no other signal.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from imp.providers.codex_config import render_config_toml

_CODEX = shutil.which("codex")

pytestmark = pytest.mark.skipif(_CODEX is None, reason="codex CLI not on PATH")

# Probe ~0.2s; 20s is huge headroom — never false-fires under load, still fails loudly if hung.
_STRICT_TIMEOUT = 20.0


def _run_strict(*, codex_home: Path, cwd: Path) -> subprocess.CompletedProcess[str]:
    assert _CODEX is not None
    return subprocess.run(
        [_CODEX, "--strict-config", "exec", "noop"],
        cwd=str(cwd),
        env={**os.environ, "CODEX_HOME": str(codex_home)},
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=_STRICT_TIMEOUT,
    )


def test_rendered_config_is_accepted_under_strict_config(tmp_path: Path) -> None:
    rendered = render_config_toml(
        host="127.0.0.1", port=49152, bearer_env_var="IMP_MCP_TOKEN",
        web_search_enabled=False, impty_url="http://127.0.0.1:55001/mcp",
        impty_bearer_env_var="IMP_IMPTY_TOKEN")
    good = tmp_path / "good"
    good.mkdir()
    (good / "config.toml").write_text(rendered)
    # Renamed exactly as codex `main` renamed it — the failure this test exists to catch.
    bad = tmp_path / "bad"
    bad.mkdir()
    (bad / "config.toml").write_text(
        rendered.replace("default_tools_approval_mode", "approval_mode"))
    # Non-git cwd: config loads, the trusted-dir check aborts before any model call; probe is free.
    nongit = tmp_path / "nongit"
    nongit.mkdir()

    ok = _run_strict(codex_home=good, cwd=nongit)
    # Exit status is NOT the signal: both configs exit 1 (on the directory check).
    assert "Error loading config.toml" not in ok.stderr, ok.stderr
    assert "unknown configuration field" not in ok.stderr, ok.stderr

    # Negative control: without it, a codex ignoring strictness makes the assertions pass vacuously.
    renamed = _run_strict(codex_home=bad, cwd=nongit)
    assert "unknown configuration field" in renamed.stderr, renamed.stderr
