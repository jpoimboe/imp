"""Symlink-safe unsandboxed reads of a live quest home's artifacts/ tree."""
from __future__ import annotations

import os
import stat
from pathlib import Path

# Agents can rewrite anything under the quest home, so never follow a link and
# never open a path by name after vetting it: walk from open directory handles.
_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
_LEAF_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC


def _open_artifacts(quest_home: Path) -> int | None:
    try:
        home = os.open(quest_home, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    except OSError:
        return None
    try:
        return os.open("artifacts", _DIR_FLAGS, dir_fd=home)
    except OSError:
        return None
    finally:
        os.close(home)


def list_files(quest_home: Path) -> list[str] | None:
    """Sorted regular files under artifacts/; None when it is absent or not a real folder."""
    art = _open_artifacts(quest_home)
    if art is None:
        return None
    try:
        out: list[str] = []
        for dirpath, _dirs, names, dir_fd in os.fwalk(
                ".", dir_fd=art, follow_symlinks=False):
            for name in names:
                try:
                    mode = os.stat(name, dir_fd=dir_fd, follow_symlinks=False).st_mode
                except OSError:
                    continue
                if stat.S_ISREG(mode):
                    out.append(os.path.normpath(os.path.join(dirpath, name)))
        return sorted(out)
    finally:
        os.close(art)


def _read_all(fd: int) -> bytes:
    chunks: list[bytes] = []
    while chunk := os.read(fd, 1 << 16):
        chunks.append(chunk)
    return b"".join(chunks)


def read_file(quest_home: Path, rel: str) -> bytes | None:
    """One regular file under artifacts/ by relative path; None on any failure."""
    *dirs, leaf = rel.split("/")
    if any(part in ("", ".", "..") for part in (*dirs, leaf)):
        return None
    cur = _open_artifacts(quest_home)
    if cur is None:
        return None
    fd: int | None = None
    try:
        for part in dirs:
            nxt = os.open(part, _DIR_FLAGS, dir_fd=cur)
            os.close(cur)
            cur = nxt
        fd = os.open(leaf, _LEAF_FLAGS, dir_fd=cur)
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return None
        return _read_all(fd)
    except OSError:
        return None
    finally:
        os.close(cur)
        if fd is not None:
            os.close(fd)
