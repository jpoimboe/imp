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
    """sha256 over sorted workflow files (path + bytes), skipping .pristine."""
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
    """Write the content hash of d to <d>/.pristine, excluding the stamp itself."""
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
    """Return drift state for a managed workflow mirror; None if name is not managed."""
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
    """Overwrite mirror_dir with the bundled workflow and re-stamp .pristine."""
    src = bundled_workflow_dir(name)
    if mirror_dir.exists():
        shutil.rmtree(mirror_dir)
    shutil.copytree(src, mirror_dir)
    if write_stamp:
        write_pristine(mirror_dir)


def workflow_update_notice(project_root: Path) -> str | None:
    """Return an update-available notice string for imp new/status, or None."""
    from imp.engine.errors import LoadError, LoadErrors
    from imp.providers.config import parse_default_workflow

    try:
        name = parse_default_workflow(project_root / "imp" / "project.yaml")
    except (LoadError, LoadErrors):
        return None
    mirror = project_root / "imp" / "workflows" / name
    if not mirror.is_dir():
        return None
    if classify_drift(name, mirror) is DriftState.UPDATE_AVAILABLE:
        return f"the {name} workflow has updates — run `imp update-workflow`."
    return None
