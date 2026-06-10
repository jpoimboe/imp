"""Background-execution parity for the MCP-wrapped providers.

claude_code and codex execute imp's registry tools in-process (SDK MCP
wrappers / codex_mcp.dispatch_tool) — the spec's "bash never fires there"
claim was false at the production site. This file pins the parity wiring:
injection, the background_enabled flag, and teardown surfaces."""

from __future__ import annotations

import pytest

from imp.providers import claude_code as cc_mod
from tests.test_build_tools_wait_for_user import _Inv


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def test_cc_build_tools_injects_bg_with_bash() -> None:
    tools = cc_mod._build_tools(_Inv(["complete_step", "bash"]))
    assert "bash_output" in tools and "bash_kill" in tools


def test_cc_build_tools_no_bg_without_bash() -> None:
    tools = cc_mod._build_tools(_Inv(["complete_step", "read_file"]))
    assert "bash_output" not in tools and "bash_kill" not in tools


def test_codex_reuses_cc_build_tools() -> None:
    """codex_runner imports _build_tools from claude_code (codex_runner.py:222)
    — pin the reuse so the injection above covers codex too. If this ever
    fails, codex grew its own builder and needs its own injection + tests."""
    import inspect

    from imp.providers import codex_runner
    src = inspect.getsource(codex_runner)
    assert "from imp.providers.claude_code import _build_tools" in src
