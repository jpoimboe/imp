from __future__ import annotations

from imp.providers.codex_exec import ThreadOptions
from imp.providers.codex_runner import CodexAgentRunner


def test_thread_options_pin_read_only_sandbox() -> None:
    """The session must be sandboxed: codex's own read-only mode is what refuses a
    native apply_patch / exec write, since approval_policy 'never' leaves no
    escalation path."""
    opts = CodexAgentRunner(model_name="gpt-6-luna")._build_thread_options()
    assert isinstance(opts, ThreadOptions)
    assert opts.sandbox_mode == "read-only"
    assert opts.approval_policy == "never"
