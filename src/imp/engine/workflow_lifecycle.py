"""Managed-workflow lifecycle: content hash, .pristine stamp, drift detection,
update notice, and the update-workflow mechanics (design §2-§4c)."""

from __future__ import annotations

import hashlib
import shutil
from enum import Enum
from pathlib import Path

from imp.assets import bundled_workflow_dir, is_bundled_workflow

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


class DriftState(Enum):
    UP_TO_DATE = "up-to-date"
    UPDATE_AVAILABLE = "update-available"
    LOCALLY_MODIFIED = "locally-modified"


def classify_drift(name: str, mirror_dir: Path) -> DriftState | None:
    """Classify a managed workflow mirror from the bundled (B), pristine (P), and
    on-disk (M) hashes. Returns None when `name` is NOT managed (no bundled twin),
    so a user fork is never a candidate. A managed mirror with no/mismatched
    stamp is locally-modified: we refuse to prove it clean, so it is never offered
    a clobbering update."""
    if not is_bundled_workflow(name):
        return None
    b = hash_workflow_dir(bundled_workflow_dir(name))
    m = hash_workflow_dir(mirror_dir)
    p = read_pristine(mirror_dir)
    if p is None or m != p:
        return DriftState.LOCALLY_MODIFIED
    if b != p:
        return DriftState.UPDATE_AVAILABLE
    return DriftState.UP_TO_DATE


def perform_workflow_update(name: str, mirror_dir: Path, *, write_stamp: bool = True) -> None:
    """Copy the bundled `name` workflow over `mirror_dir` and re-stamp .pristine.
    Caller must classify before calling (should not be called on a LOCALLY_MODIFIED
    or non-managed mirror)."""
    src = bundled_workflow_dir(name)
    if mirror_dir.exists():
        shutil.rmtree(mirror_dir)
    shutil.copytree(src, mirror_dir)
    if write_stamp:
        write_pristine(mirror_dir)


def workflow_update_notice(project_root: Path) -> str | None:
    """One-line surface for `imp new`/`imp status`: the message to print when the
    project's default-workflow mirror is update-available, else None."""
    from imp.providers.config import parse_default_workflow

    name = parse_default_workflow(project_root / "imp" / "project.yaml")
    mirror = project_root / "imp" / "workflows" / name
    if not mirror.is_dir():
        return None
    if classify_drift(name, mirror) is DriftState.UPDATE_AVAILABLE:
        return f"the {name} workflow has updates — run `imp update-workflow`."
    return None
