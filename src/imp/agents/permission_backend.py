"""Decorator backend that enforces fs_permissions rules before delegating.

The deepagents CompositeBackend routes /artifacts/, /project/, etc. to
separate FilesystemBackend instances. From the inner backend's perspective,
a path is rooted at its mount: a write to /project/foo.py arrives at the
project FilesystemBackend as /foo.py.

This wrapper holds the mount prefix and re-prepends it before evaluating
rules, so workflow authors write rules in the agent-facing path namespace.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from imp.agents.permissions import check_permission
from imp.engine.types import FsRule


class PermissionFilteringBackend:
    def __init__(
        self,
        inner: Any,
        rules: Sequence[FsRule],
        *,
        prefix: str,
    ) -> None:
        self._inner = inner
        self._rules = tuple(rules)
        # Always ends with "/"; normalize accidental inputs
        self._prefix = prefix if prefix.endswith("/") else prefix + "/"

    def _abs(self, path: str) -> str:
        if path.startswith("/"):
            path = path[1:]
        return self._prefix + path

    def read(self, path: str) -> str:
        check_permission(self._rules, "read", self._abs(path))
        return self._inner.read(path)

    def write(self, path: str, content: str) -> Any:
        check_permission(self._rules, "write", self._abs(path))
        return self._inner.write(path, content)

    def edit(self, path: str, *args: Any, **kwargs: Any) -> Any:
        check_permission(self._rules, "write", self._abs(path))
        return self._inner.edit(path, *args, **kwargs)

    # Untouched passthroughs — read/write are the only gated ops in V0
    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)
