"""The one unprivileged-overlay probe the sandbox test modules gate on.

A helper module, not a collected one (pytest's python_files is test_*.py), so
importing it runs exactly ONE probe per pytest process. The verbatim skip-reason
assertions live in tests/test_sandbox_behavioral.py, which pytest does collect.
"""
from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

import pytest

from imp.engine.sandbox import overlay_mode, sandbox_available

# The overlay home under test must live at a path covered by NO later bind_plan
# bind — NOT under the per-quest scratch dir (its rw bind would re-mount over an
# overlay home placed there, defeating it) and NOT under the sandbox worktree (its
# rw bind does the same). SandboxPolicy.bind_plan emits the home-layer rw bind
# FIRST, so it can never be the last-covering bind for a path that a later bind also
# covers. So the overlay tests root their home AND worktree as SIBLINGS under a
# real fs OUTSIDE /tmp (/var/tmp): the --ro-bind / / floor keeps them readable, and
# the home-layer rw bind is then the ONLY bind covering the home path, so the
# overlay isolates correctly (writes captured in the upper; real home untouched).
# /var/tmp is also a real disk fs — a valid overlay upperdir location, unlike a
# possible tmpfs /tmp.
OVERLAY_BASE = Path("/var/tmp")


def _probe_overlay_at_base() -> bool | None:
    """Probe overlay support at the /var/tmp base, removing the probe dirs after,
    and return overlay_mode's tri-state: None → no unprivileged overlay here (for
    ANY reason, including no bwrap and no /var/tmp); True/False → the mount shape
    this host accepts. overlay_mode() only rmtrees its own inner .overlay-probe
    subdir; the mkdtemp'd probe/home parents are cleaned up here. `home` is a
    separate empty dir (non-overlapping with the probe's scratch layers) so a bare
    host mounts it cleanly — the same real-home lowerdir shape the per-command
    mount uses. That lowerdir is a plain directory, never a mount point, so the
    probe still succeeds under a nested sandbox whose $HOME is itself an overlay."""
    if not sandbox_available() or not OVERLAY_BASE.is_dir():
        return None
    try:
        probe = Path(tempfile.mkdtemp(prefix="imp-ovl-probe-", dir=OVERLAY_BASE))
        home = Path(tempfile.mkdtemp(prefix="imp-ovl-home-", dir=OVERLAY_BASE))
    except OSError:
        return None
    try:
        return overlay_mode(probe, home)
    finally:
        shutil.rmtree(probe, ignore_errors=True)
        shutil.rmtree(home, ignore_errors=True)


OVERLAY_MODE: bool | None = _probe_overlay_at_base()

requires_overlay = pytest.mark.skipif(
    OVERLAY_MODE is None,
    reason="unprivileged overlayfs probe at /var/tmp failed (read-only base or "
           "stacked overlay under a nested sandbox)")

requires_userxattr_overlay = pytest.mark.skipif(
    OVERLAY_MODE is not True,
    reason="unprivileged overlayfs here does not accept -o userxattr (kernel < v5.11); "
           "the opaque-marker fix cannot apply")
