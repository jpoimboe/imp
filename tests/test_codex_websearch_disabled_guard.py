"""Regression guard for the Finding A fix (`b0e1658`).

imp disables model-hosted web search via config.toml's top-level `web_search`
key and must NEVER emit the SDK's deprecated `--config
features.web_search_request` flag — codex >=0.137 ignores it for disabling, so
emitting it re-opens a web side channel that bypasses the
`networkAccessEnabled=False` sandbox lock. The flag is emitted by the SDK iff
`ThreadOptions.web_search_enabled is not None` (openai_codex_sdk exec.py), so the
invariant is: imp's ThreadOptions leaves it None on every invocation, regardless
of whether the agent declared the `web_search` tool.

This is a structural guard so a future `web_search_enabled=` can't silently
re-introduce the bug behind a live-only check.
"""

from __future__ import annotations

import importlib.util

import pytest

from imp.providers.codex_runner import CodexAgentRunner

pytestmark = pytest.mark.skipif(
    importlib.util.find_spec("openai_codex_sdk") is None,
    reason="openai_codex_sdk not importable",
)


def test_thread_options_never_set_web_search_enabled() -> None:
    opts = CodexAgentRunner(model_name="gpt-5.5")._build_thread_options()
    assert opts is not None
    # The imp-side invariant: control is via config.toml, never this SDK flag.
    assert opts.web_search_enabled is None
    assert "webSearchEnabled" not in opts.model_dump(exclude_none=True)


def test_sdk_args_never_emit_deprecated_web_search_flag() -> None:
    # End-to-end at the SDK seam: the command args the SDK builds from imp's
    # ThreadOptions must not contain the deprecated flag (the exact bug b0e1658
    # closed), whether or not a resume thread_id is present.
    from openai_codex_sdk.exec import CodexExec, CodexExecArgs

    opts = CodexAgentRunner(model_name="gpt-5.5")._build_thread_options()
    exec_ = CodexExec(executable_path="codex")  # dummy path; args build is pure
    for thread_id in (None, "thr-resume-1"):
        args = CodexExecArgs(
            input="x",
            thread_id=thread_id,
            model=opts.model,
            sandbox_mode=opts.sandbox_mode,
            web_search_enabled=opts.web_search_enabled,
            network_access_enabled=opts.network_access_enabled,
        )
        cmd = exec_._build_command_args(args)
        assert not any("features.web_search_request" in a for a in cmd), cmd
