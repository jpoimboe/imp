from __future__ import annotations

import importlib.util

import pytest

from imp.providers.codex_runner import CodexAgentRunner

pytestmark = pytest.mark.skipif(
    importlib.util.find_spec("openai_codex_sdk") is None,
    reason="openai_codex_sdk not importable",
)


def test_thread_options_pin_read_only_sandbox() -> None:
    """The session must be sandboxed: codex's own read-only mode is what refuses a
    native apply_patch / exec write, since approval_policy 'never' leaves no
    escalation path."""
    opts = CodexAgentRunner(model_name="gpt-5.6-luna")._build_thread_options()
    assert opts is not None
    assert opts.sandbox_mode == "read-only"
    assert opts.approval_policy == "never"
    assert opts.network_access_enabled is False
    dumped = opts.model_dump(by_alias=True, exclude_none=True)
    assert dumped["sandboxMode"] == "read-only"
    assert dumped["approvalPolicy"] == "never"
    assert dumped["networkAccessEnabled"] is False
