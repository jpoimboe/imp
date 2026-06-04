"""Engine-provided environment variables.

These are the variables the engine guarantees to every subprocess it spawns on
behalf of a Change — script gates (`run_script_gate`) and the agent `bash` tool.
Centralizing them here keeps the two execution paths consistent and gives future
engine vars a single home.

All paths are host paths. There is no sandbox: the `/project/` prefix used in
`fs_permissions` is a virtual namespace only, never a real mount point.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path

# Every env var this module manages. `engine_env` is the AUTHORITY for these: a
# subprocess must see exactly what `engine_env` produces, never an inherited copy.
# `IMP_LINT_COMMAND` and `IMP_TEST_COMMAND` are conditionally present
# (absent => no lint/test gate), so without explicit stripping a stale ambient
# value (e.g. from a parent imp process during imp-on-imp dogfooding) would leak
# through a `{**os.environ, ...}` merge and falsely enable the gate. Use
# `overlay_engine_env` to merge, not a raw dict spread.
ENGINE_ENV_VARS = frozenset({
    "IMP_PROJECT_ROOT",
    "IMP_CHANGE_ID",
    "IMP_CHANGE_SLUG",
    "IMP_CHANGE_TITLE",
    "IMP_LINT_COMMAND",
    "IMP_TEST_COMMAND",
})


def overlay_engine_env(
    parent: Mapping[str, str], overlay: Mapping[str, str],
) -> dict[str, str]:
    """Merge `overlay` (an `engine_env` result) onto `parent`, authoritatively.

    Any engine-managed var inherited from `parent` is dropped first, so only the
    vars `overlay` actually provides reach the subprocess. This prevents a stale
    ambient engine var — notably `IMP_LINT_COMMAND` or `IMP_TEST_COMMAND` from an
    outer imp process — from leaking past `engine_env`'s "absent => no lint/test
    gate" contract.

    `PYTHONPATH` is special-cased: it is a *prefix*, not an authoritative value.
    The worktree's `src/` (set by `engine_env`) is prepended to any inherited
    `PYTHONPATH` so the worktree's own sources win over an editable install that
    pins a sibling checkout — without discarding ambient entries the parent set.
    """
    env = {k: v for k, v in parent.items() if k not in ENGINE_ENV_VARS}
    overlay = dict(overlay)
    pythonpath_prefix = overlay.pop("PYTHONPATH", None)
    env.update(overlay)
    if pythonpath_prefix:
        inherited = parent.get("PYTHONPATH")
        env["PYTHONPATH"] = (
            pythonpath_prefix if not inherited
            else pythonpath_prefix + os.pathsep + inherited
        )
    return env


def engine_env(
    *, project_root: Path, change_id: str, change_slug: str, change_title: str,
    lint_command: str | None = None,
    test_command: str | None = None,
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
        IMP_LINT_COMMAND  — optional lint-clean gate command, injected only when
                            configured. A falsy command (None or empty string) is
                            treated as absent => no lint gate, so the var is omitted.
        IMP_TEST_COMMAND  — optional test-run gate command, injected only when
                            configured. A falsy command (None or empty string) is
                            treated as absent => no test gate, so the var is omitted.
        PYTHONPATH        — prefix pointing at `<project_root>/src`, injected only
                            when that directory exists. `overlay_engine_env`
                            prepends it to any inherited PYTHONPATH so subprocesses
                            import the worktree's own packages instead of resolving
                            an editable install to a sibling checkout. Emitted as a
                            bare prefix here; the prepend-vs-clobber merge happens
                            in `overlay_engine_env` where the parent env is visible.
    """
    env = {
        "IMP_PROJECT_ROOT": str(project_root),
        "IMP_CHANGE_ID": change_id,
        "IMP_CHANGE_SLUG": change_slug,
        "IMP_CHANGE_TITLE": change_title,
    }
    if lint_command:
        env["IMP_LINT_COMMAND"] = lint_command
    if test_command:
        env["IMP_TEST_COMMAND"] = test_command
    # Editable installs (pip install -e) pin the package to whatever checkout was
    # installed — for imp-on-imp that is the parent repo, so a worktree's `import
    # imp` resolves to the parent's src, not the worktree's. Prepending the
    # worktree's own `src/` to PYTHONPATH shadows that. Guarded on existence so it
    # is a no-op for projects without a `src/` layout.
    src_dir = project_root / "src"
    if src_dir.is_dir():
        env["PYTHONPATH"] = str(src_dir)
    return env
