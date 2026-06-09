"""Codex backend provider (spec: `2026-06-05-add-codex-provider`).

Drives the OpenAI Codex CLI via `openai-codex-sdk` as a brain that calls imp's
tools over an in-process streamable-HTTP MCP server. Mirrors `claude_code.py`'s
runner shape; the differences are in this module's siblings:

  - `codex_mcp.py`     — the MCP server + tool wrappers + cap counter.
  - `codex_config.py`  — per-run CODEX_HOME, config.toml, auth seed + reconcile.

Soft enforcement against native `apply_patch` (#8161 still unfixed in 0.129.0):
the system prompt forbids native tools; a `file_change` / `command_execution`
item is a fail-closed tripwire that reverts the worktree to run-entry state.

`_env_check` requires BOTH `codex` on PATH AND `openai_codex_sdk` importable —
stricter than `claude_code._env_check` (binary-only). Deliberate asymmetry.
"""

from __future__ import annotations

import importlib.util
import shutil
import sys
from typing import Any

from imp.agents.runner import AgentInvocation, AgentRunner
from imp.providers.base import Provider

# codex refuses *-codex models (runner preflight M1); gpt-5.5 is the sanctioned
# non-codex model at every tier.
TIER_DEFAULTS: dict[str, str] = {"low": "gpt-5.5", "medium": "gpt-5.5", "high": "gpt-5.5"}
ENV_VAR: str | None = None  # CLI-authenticated (codex login); no env var


def _env_check() -> bool:
    """True iff both the codex CLI and the Python SDK are present."""
    if shutil.which("codex") is None:
        return False
    # find_spec raises ValueError when the module is already in sys.modules but
    # its __spec__ is None (e.g. a bare ModuleType stub inserted during tests).
    # Fall back to a sys.modules membership check in that case.
    try:
        return importlib.util.find_spec("openai_codex_sdk") is not None
    except ValueError:
        return "openai_codex_sdk" in sys.modules


class CodexProvider:
    name = "codex"

    def __init__(self, model_name: str, *, client_factory: Any = None) -> None:
        self.model = model_name
        self._client_factory = client_factory

    def create_runner(self, invocation: AgentInvocation) -> AgentRunner:
        from imp.providers.codex_runner import CodexAgentRunner
        return CodexAgentRunner(
            model_name=self.model, client_factory=self._client_factory,
            user_input_fn=invocation.user_input_fn,
        )


def _factory(model: str) -> Provider:
    return CodexProvider(model_name=model)
