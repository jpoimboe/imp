"""Managed-workflow lifecycle: content hash, .pristine stamp, drift detection,
update notice, and the update-workflow mechanics (design §2-§4c)."""

from __future__ import annotations

import hashlib
from pathlib import Path

PRISTINE_FILENAME = ".pristine"


def hash_workflow_dir(d: Path) -> str:
    """sha256 over the dir's files in sorted relative-path order — each file's
    posix relative path, then its bytes — skipping the .pristine stamp and any
    non-file entries. The stamp cannot hash itself."""
    h = hashlib.sha256()
    files = sorted(
        (
            p for p in d.rglob("*")
            if p.is_file() and p.relative_to(d).as_posix() != PRISTINE_FILENAME
        ),
        key=lambda p: p.relative_to(d).as_posix(),
    )
    for p in files:
        h.update(p.relative_to(d).as_posix().encode("utf-8"))
        h.update(b"\0")
        h.update(p.read_bytes())
        h.update(b"\0")
    return h.hexdigest()


def write_pristine(d: Path) -> None:
    """Record the content hash of `d` into <d>/.pristine (computed excluding the
    stamp itself, so a re-stamp is idempotent)."""
    (d / PRISTINE_FILENAME).write_text(hash_workflow_dir(d) + "\n")


def read_pristine(d: Path) -> str | None:
    """Return the recorded pristine hash, or None when the stamp is absent."""
    p = d / PRISTINE_FILENAME
    if not p.is_file():
        return None
    return p.read_text().strip()
