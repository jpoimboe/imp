"""Decorator backend that enforces fs_permissions rules before delegating.

The deepagents CompositeBackend routes /artifacts/, /project/, etc. to
separate FilesystemBackend instances. From the inner backend's perspective,
a path is rooted at its mount: a write to /project/foo.py arrives at the
project FilesystemBackend as /foo.py.

This wrapper holds the mount prefix and re-prepends it before evaluating
rules, so workflow authors write rules in the agent-facing path namespace.
"""

from __future__ import annotations

import posixpath
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
        combined = self._prefix + path
        # Normalize ../ and ./ before rule evaluation. Use posixpath so the
        # behavior is OS-independent (paths in our rules are always POSIX).
        return posixpath.normpath(combined)

    # All gated methods accept *args/**kwargs and forward verbatim so any
    # extra options the inner FilesystemBackend understands (offset, limit,
    # encoding, etc.) pass through untouched.

    def read(self, path: str, *args: Any, **kwargs: Any) -> Any:
        check_permission(self._rules, "read", self._abs(path))
        return self._inner.read(path, *args, **kwargs)

    def write(self, path: str, *args: Any, **kwargs: Any) -> Any:
        check_permission(self._rules, "write", self._abs(path))
        return self._inner.write(path, *args, **kwargs)

    def edit(self, path: str, *args: Any, **kwargs: Any) -> Any:
        check_permission(self._rules, "write", self._abs(path))
        return self._inner.edit(path, *args, **kwargs)

    async def aread(self, path: str, *args: Any, **kwargs: Any) -> Any:
        check_permission(self._rules, "read", self._abs(path))
        return await self._inner.aread(path, *args, **kwargs)

    async def awrite(self, path: str, *args: Any, **kwargs: Any) -> Any:
        check_permission(self._rules, "write", self._abs(path))
        return await self._inner.awrite(path, *args, **kwargs)

    async def aedit(self, path: str, *args: Any, **kwargs: Any) -> Any:
        check_permission(self._rules, "write", self._abs(path))
        return await self._inner.aedit(path, *args, **kwargs)

    # Untouched passthroughs — read/write are the only gated ops in V0
    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)
