# SPDX-License-Identifier: MIT
# Copyright 2026 Josh Poimboeuf
"""Codex backend provider (spec: `2026-06-05-add-codex-provider`).

Drives the OpenAI Codex CLI as a brain that calls imp's tools over an
in-process streamable-HTTP MCP server. Mirrors `claude_code.py`'s runner shape;
the differences are in this module's siblings:

  - `codex_exec.py`    — the `codex exec --json` spawn, read loop, and teardown.
  - `codex_events.py`  — the wire event models and the parse boundary.
  - `codex_mcp.py`     — the MCP server + tool wrappers + cap counter.
  - `codex_config.py`  — per-run CODEX_HOME, config.toml, auth seed + reconcile.

Native `apply_patch` is refused by the read-only sandbox — no approval path
under `approval_policy = "never"`. The shell vector is additionally removed
outright by `[features] shell_tool = false`, which leaves the `exec_command`
binding absent before the sandbox is consulted. imp's own MCP tools run in
imp's process, outside that sandbox, and are auto-approved per server via
config.toml.

`_env_check` requires only `codex` on PATH, matching `claude_code._env_check`.
"""

from __future__ import annotations

import shutil
from typing import Any

from imp.agents.runner import AgentInvocation, AgentRunner
from imp.providers.base import Provider


def _env_check() -> bool:
    """True iff the codex CLI is on PATH."""
    return shutil.which("codex") is not None


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
