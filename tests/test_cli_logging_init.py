"""`_init_logging` must claim the root logger before any FastMCP server is
built, so FastMCP's `configure_logging()` (a stderr RichHandler at INFO) can't
hijack it and paint third-party INFO logs over the live TUI.

These run in clean subprocesses: pytest's own logging plugin installs a
LogCaptureHandler on the root logger, so an in-process test can't observe the
empty-root condition that `_init_logging` keys on (and that `main()` sees for
real). See `imp.cli.main._init_logging`.
"""
from __future__ import annotations

import subprocess
import sys


def _run(script: str, **env_extra: str) -> subprocess.CompletedProcess[str]:
    import os

    env = {**os.environ, **env_extra}
    return subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, env=env)


def test_init_logging_defuses_fastmcp_hijack() -> None:
    # Importing impty.mcp instantiates FastMCP("impty"), whose __init__ calls
    # configure_logging() → basicConfig(RichHandler) when root has no handler.
    # Our NullHandler must make that a no-op.
    script = """
import logging
from imp.cli.main import _init_logging
_init_logging()
import impty.mcp  # side effect: FastMCP("impty")
root = logging.getLogger()
types = sorted(type(h).__name__ for h in root.handlers)
assert types == ["NullHandler"], types
assert root.level == logging.WARNING, root.level
print("OK")
"""
    r = _run(script)
    assert r.returncode == 0, r.stderr
    assert "OK" in r.stdout


def test_init_logging_quiets_noisy_sdk_logger() -> None:
    script = """
import logging
from imp.cli.main import _init_logging
_init_logging()
sdk = logging.getLogger("claude_agent_sdk._internal.transport.subprocess_cli")
assert sdk.getEffectiveLevel() >= logging.WARNING, sdk.getEffectiveLevel()
print("OK")
"""
    r = _run(script)
    assert r.returncode == 0, r.stderr
    assert "OK" in r.stdout


def test_init_logging_does_not_clobber_existing_handlers() -> None:
    script = """
import logging
from imp.cli.main import _init_logging
sentinel = logging.StreamHandler()
logging.getLogger().addHandler(sentinel)
_init_logging()
assert logging.getLogger().handlers == [sentinel]
print("OK")
"""
    r = _run(script)
    assert r.returncode == 0, r.stderr
    assert "OK" in r.stdout


def test_init_logging_debug_env_logs_to_stderr() -> None:
    script = """
import logging
from imp.cli.main import _init_logging
_init_logging()
root = logging.getLogger()
assert any(isinstance(h, logging.StreamHandler) for h in root.handlers), root.handlers
assert root.level == logging.DEBUG, root.level
print("OK")
"""
    r = _run(script, IMP_DEBUG="1")
    assert r.returncode == 0, r.stderr
    assert "OK" in r.stdout
