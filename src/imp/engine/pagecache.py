"""Best-effort page-cache eviction for transient read-fault recovery.

Forensics (docs/superpowers/specs/2026-06-28-state-load-hardening-design.md):
btrfs+zstd can transiently serve a mis-decompressed cached copy of a file that
is valid on disk — the observed signature is the content minus its first byte
(head-drop). Dropping the file's cached pages forces the next read to re-fetch
the extent from disk, which recovers.
"""

from __future__ import annotations

import os
from pathlib import Path


def drop_page_cache(path: Path) -> None:
    """Ask the kernel (POSIX_FADV_DONTNEED) to drop this file's cached pages so
    the next read re-fetches from disk rather than serving the cached copy that
    just failed to parse. Best-effort; never raises."""
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.posix_fadvise(fd, 0, 0, os.POSIX_FADV_DONTNEED)
    except (OSError, AttributeError, ValueError):
        pass
    finally:
        os.close(fd)
