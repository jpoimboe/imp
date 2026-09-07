# SPDX-License-Identifier: MIT
# Copyright 2026 Josh Poimboeuf
"""imp's driver for the `codex exec --json` subprocess.

Owns argv/env construction, the spawn, the JSONL read loop, the concurrent stderr
drain, and the group-reaping teardown. The child is its own session leader, so a
terminal Ctrl-C (a process-group SIGINT) reaches imp alone, and teardown can kill
the child's whole group rather than orphaning its grandchildren.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

# 64 MiB ceiling for one JSONL line: asyncio's 64 KiB default makes
# StreamReader.readline() raise on a larger codex event line.
CODEX_STREAM_LIMIT = 64 * 1024 * 1024
INTERNAL_ORIGINATOR_ENV = "CODEX_INTERNAL_ORIGINATOR_OVERRIDE"
# Server-visible: what OpenAI's backend sees. Not this quest's experiment.
CODEX_ORIGINATOR = "codex_sdk_py"

SandboxMode = Literal["read-only", "workspace-write", "danger-full-access"]
ApprovalMode = Literal["never", "on-request", "on-failure", "untrusted"]
ModelReasoningEffort = Literal["minimal", "low", "medium", "high"]


@dataclass(frozen=True)
class CodexOptions:
    env: dict[str, str]


@dataclass(frozen=True)
class ThreadOptions:
    model: str
    sandbox_mode: SandboxMode
    approval_policy: ApprovalMode
    network_access_enabled: bool
    model_reasoning_effort: ModelReasoningEffort | None = None


def build_command_args(options: ThreadOptions, thread_id: str | None) -> list[str]:
    """The argv after the executable. Takes no prompt: it travels on stdin, so it
    cannot leak into a process listing."""
    args = ["exec", "--json", "--model", options.model, "--sandbox", options.sandbox_mode]
    if options.model_reasoning_effort is not None:
        args += ["--config", f'model_reasoning_effort="{options.model_reasoning_effort}"']
    network = "true" if options.network_access_enabled else "false"
    args += [
        "--config", f"sandbox_workspace_write.network_access={network}",
        "--config", f'approval_policy="{options.approval_policy}"',
    ]
    if thread_id is not None:
        args += ["resume", thread_id]
    return args
