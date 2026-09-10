"""Regression guard for the Finding A fix (`b0e1658`).

imp disables model-hosted web search via config.toml's top-level `web_search`
key and must NEVER emit the deprecated `--config features.web_search_request`
flag — codex >=0.137 ignores it for disabling, so emitting it re-opens a web
side channel that bypasses the `network_access_enabled=False` sandbox lock.
imp's own ThreadOptions carries no web-search field at all, so the flag is
structurally unemittable; this file pins that at the argv the runner's real
options produce, resume leg included.
"""

from __future__ import annotations

import pytest

from imp.providers.codex_exec import build_command_args
from imp.providers.codex_runner import CodexAgentRunner


def test_runner_options_build_the_production_argv_with_no_web_search_flag() -> None:
    opts = CodexAgentRunner(model_name="gpt-5.5")._build_thread_options("high")
    assert build_command_args(opts, None) == [
        "exec", "--json", "--model", "gpt-5.5", "--sandbox", "read-only",
        "--config", 'model_reasoning_effort="high"',
        "--config", "sandbox_workspace_write.network_access=false",
        "--config", 'approval_policy="never"']
    for thread_id in (None, "thr-resume-1"):
        args = build_command_args(opts, thread_id)
        assert not any("web_search" in a for a in args), args


@pytest.mark.parametrize(
    ("tier", "model"),
    [("medium", "gpt-5.6-terra"), ("high", "gpt-6-astra")],
)
def test_thinking_tiers_pin_high_reasoning_effort(tier: str, model: str) -> None:
    opts = CodexAgentRunner(model_name=model)._build_thread_options(tier)
    assert opts.model == model
    assert opts.model_reasoning_effort == "high"
    assert opts.model_reasoning_effort not in {"xhigh", "max", "ultra"}


@pytest.mark.parametrize("tier", ["low", None])
def test_low_and_untiered_leave_reasoning_effort_unset(tier: str | None) -> None:
    opts = CodexAgentRunner(model_name="gpt-5.6-luna")._build_thread_options(tier)
    assert opts.model_reasoning_effort is None
