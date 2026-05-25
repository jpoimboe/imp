"""Composite filesystem backend for deepagents (spec §6.4).

Per Spike 4 (spikes/spike4_backend.py):
  CompositeBackend(default, routes={prefix: backend, ...}, artifacts_root="/")
  FilesystemBackend(root_dir, virtual_mode=True)  # virtual_mode REQUIRED
  StateBackend()  # in-memory; lives in deepagent state

Routes:
  default          → StateBackend (anything not matching a prefix)
  /artifacts/*     → FilesystemBackend(change_artifacts_dir)
  /project/*       → FilesystemBackend(project_root)

virtual_mode=True is required: without it, absolute paths bypass root_dir
entirely (security) AND CompositeBackend's prefix-stripping doesn't work
correctly (functionality).
"""

from __future__ import annotations

from typing import Any

from deepagents.backends import (
    CompositeBackend,
    FilesystemBackend,
    StateBackend,
)

from imp.agents.runner import AgentInvocation


def build_backend(invocation: AgentInvocation) -> Any:
    return CompositeBackend(
        default=StateBackend(),
        routes={
            "/artifacts/": FilesystemBackend(
                root_dir=str(invocation.change_artifacts_dir),
                virtual_mode=True,
            ),
            "/project/": FilesystemBackend(
                root_dir=str(invocation.project_root),
                virtual_mode=True,
            ),
        },
    )
