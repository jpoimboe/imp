# SPDX-License-Identifier: MIT
# Copyright 2026 Josh Poimboeuf
"""Pluggable content-addressed blob store (spec: blob store).

The interface keeps the team/server door open; only the local-directory backend
is implemented in Phase 1. Blobs are keyed by the sha256 hexdigest of their
content, so writing identical bytes is deterministic and dedup'ing.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Protocol

from imp.engine import durable


def hash_content(content: bytes) -> str:
    """The content-address (sha256 hexdigest) of a blob's bytes."""
    return hashlib.sha256(content).hexdigest()


class BlobStore(Protocol):
    def put(self, content: bytes) -> str: ...
    def get(self, digest: str) -> bytes | None: ...
    def has(self, digest: str) -> bool: ...


class LocalBlobStore:
    """Local-directory backend. Blobs live at <root>/<digest[:2]>/<digest[2:]>
    (git-style sharding to bound directory fan-out). Selected by default with no
    configuration required."""

    def __init__(self, root: Path) -> None:
        self.root = root

    def _path_for(self, digest: str) -> Path:
        return self.root / digest[:2] / digest[2:]

    def put(self, content: bytes) -> str:
        digest = hash_content(content)
        dest = self._path_for(digest)
        self.root.mkdir(parents=True, exist_ok=True)
        dest.parent.mkdir(exist_ok=True)
        if dest.exists():
            durable.fsync_file(dest)
            durable.fsync_directory(dest.parent)
        else:
            durable.durable_replace_bytes(dest, content)
        durable.fsync_directory(self.root)
        durable.fsync_directory(self.root.parent)
        return digest

    def has(self, digest: str) -> bool:
        return self._path_for(digest).exists()

    def get(self, digest: str) -> bytes | None:
        dest = self._path_for(digest)
        return dest.read_bytes() if dest.exists() else None
