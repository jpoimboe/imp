"""Engine-provided environment variables.

These are the variables the engine guarantees to every subprocess it spawns on
behalf of a Change — script gates (`run_script_gate`) and the agent `bash` tool.
Centralizing them here keeps the two execution paths consistent and gives future
engine vars a single home.

All paths are host paths. `IMP_PROJECT_ROOT` is the absolute worktree path on the
host; `fs_permissions` patterns are worktree-relative (no `/project` namespace).
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path

# Every env var this module manages. `engine_env` is the AUTHORITY for these: a
# subprocess must see exactly what `engine_env` produces, never an inherited copy.
# `IMP_VERIFY_COMMAND` is conditionally present (absent => no verify gate), so
# without explicit stripping a stale ambient value (e.g. from a parent imp process
# during imp-on-imp dogfooding) would leak through a `{**os.environ, ...}` merge
# and falsely enable the gate. Use `overlay_engine_env` to merge, not a raw dict spread.
ENGINE_ENV_VARS = frozenset({
    "IMP_PROJECT_ROOT",
    "IMP_CHANGE_ID",
    "IMP_CHANGE_SLUG",
    "IMP_CHANGE_TITLE",
    "IMP_VERIFY_COMMAND",
})


def overlay_engine_env(
    parent: Mapping[str, str], overlay: Mapping[str, str],
) -> dict[str, str]:
    """Merge `overlay` (an `engine_env` result) onto `parent`, authoritatively.

    Any engine-managed var inherited from `parent` is dropped first, so only the
    vars `overlay` actually provides reach the subprocess. This prevents a stale
    ambient engine var — notably `IMP_VERIFY_COMMAND` from an outer imp process —
    from leaking past `engine_env`'s "absent => no verify gate" contract.

    `PYTHONPATH` is special-cased: it is a *prefix*, not an authoritative value.
    The worktree's `src/` (set by `engine_env` for an imp checkout) is prepended
    to any inherited `PYTHONPATH` so the worktree's own sources win over an
    editable install that pins a sibling checkout — without discarding ambient
    entries the parent set, and without duplicating a copy already inherited.
    """
    env = {k: v for k, v in parent.items() if k not in ENGINE_ENV_VARS}
    overlay = dict(overlay)
    pythonpath_prefix = overlay.pop("PYTHONPATH", None)
    env.update(overlay)
    if pythonpath_prefix:
        inherited = parent.get("PYTHONPATH", "")
        # Prepend, dropping any empty or duplicate copy of the prefix the parent
        # already carried (nested imp-on-imp, or a re-entered worktree) so the
        # path doesn't grow a redundant entry each level.
        rest = [p for p in inherited.split(os.pathsep)
                if p and p != pythonpath_prefix]
        env["PYTHONPATH"] = os.pathsep.join([pythonpath_prefix, *rest])
    return env


def engine_env(
    *, project_root: Path, change_id: str, change_slug: str, change_title: str,
    verify_command: str | None = None,
) -> dict[str, str]:
    """Return the environment variables the engine injects into every subprocess.

    Variables:
        IMP_PROJECT_ROOT  — absolute path of the project root on the host.
        IMP_CHANGE_ID     — the stable, immutable Change identity; hook authors
                            MUST key on this.
        IMP_CHANGE_SLUG   — display-only derived name; MAY change mid-session on a
                            title rename. Do not key automation on it; use
                            IMP_CHANGE_ID.
        IMP_CHANGE_TITLE  — human-readable title for the change.
        IMP_VERIFY_COMMAND — optional combined verify gate command (tests+lint+typecheck),
                            injected only when configured. A falsy command (None or
                            empty string) is treated as absent => no verify gate, so
                            the var is omitted.
        PYTHONPATH        — prefix pointing at `<project_root>/src`, injected only
                            for an imp source checkout (imp-on-imp dogfooding), so
                            unrelated projects' subprocesses are never perturbed.
                            `overlay_engine_env` prepends it to any inherited
                            PYTHONPATH so subprocesses import the worktree's own
                            imp instead of resolving an editable install to a
                            sibling checkout. Emitted as a bare prefix here; the
                            prepend-and-dedup merge happens in `overlay_engine_env`
                            where the parent env is visible.
    """
    env = {
        "IMP_PROJECT_ROOT": str(project_root),
        "IMP_CHANGE_ID": change_id,
        "IMP_CHANGE_SLUG": change_slug,
        "IMP_CHANGE_TITLE": change_title,
    }
    if verify_command:
        env["IMP_VERIFY_COMMAND"] = verify_command
    # imp-on-imp only: when the project being changed is itself an imp source
    # checkout, an editable install (pip install -e) pins `import imp` to the
    # parent repo, so a worktree's agent would import the parent's code, not the
    # worktree's. Prepending the worktree's own `src/` to PYTHONPATH shadows that.
    #
    # Scoped to an imp checkout (`_is_imp_checkout`), NOT any `src/` directory:
    # imp manages arbitrary projects, and forcing every project's `src/` onto its
    # subprocess PYTHONPATH would silently shadow modules for unrelated (even
    # non-Python) projects that merely happen to have a `src/` folder.
    #
    # The shadow relies on PYTHONPATH preceding site-packages on sys.path. That
    # holds for path-style editable installs (imp's, via pipx/`pip install -e`).
    # A strict PEP 660 editable that registers a `sys.meta_path` finder would
    # resolve first and defeat it — out of scope while imp ships path-style.
    if _is_imp_checkout(project_root):
        env["PYTHONPATH"] = str(project_root / "src")
    return env


def _is_imp_checkout(project_root: Path) -> bool:
    """True when `project_root` is an imp source checkout (src-layout with the
    imp package present). Gates the imp-on-imp PYTHONPATH shadow so unrelated
    projects' subprocesses are never perturbed."""
    return (project_root / "src" / "imp" / "__init__.py").is_file()
