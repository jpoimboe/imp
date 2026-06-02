"""Engine-provided environment variables.

These are the variables the engine guarantees to every subprocess it spawns on
behalf of a Change — script gates (`run_script_gate`) and the agent `bash` tool.
Centralizing them here keeps the two execution paths consistent and gives future
engine vars a single home.

All paths are host paths. There is no sandbox: the `/project/` prefix used in
`fs_permissions` is a virtual namespace only, never a real mount point.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

# Every env var this module manages. `engine_env` is the AUTHORITY for these: a
# subprocess must see exactly what `engine_env` produces, never an inherited copy.
# `IMP_LINT_COMMAND` is conditionally present (absent => no lint gate), so without
# explicit stripping a stale ambient value (e.g. from a parent imp process during
# imp-on-imp dogfooding) would leak through a `{**os.environ, ...}` merge and
# falsely enable the gate. Use `overlay_engine_env` to merge, not a raw dict spread.
ENGINE_ENV_VARS = frozenset({
    "IMP_PROJECT_ROOT",
    "IMP_CHANGE_SLUG",
    "IMP_CHANGE_TITLE",
    "IMP_LINT_COMMAND",
})


def overlay_engine_env(
    parent: Mapping[str, str], overlay: Mapping[str, str],
) -> dict[str, str]:
    """Merge `overlay` (an `engine_env` result) onto `parent`, authoritatively.

    Any engine-managed var inherited from `parent` is dropped first, so only the
    vars `overlay` actually provides reach the subprocess. This prevents a stale
    ambient engine var — notably `IMP_LINT_COMMAND` from an outer imp process —
    from leaking past `engine_env`'s "absent => no lint gate" contract.
    """
    env = {k: v for k, v in parent.items() if k not in ENGINE_ENV_VARS}
    env.update(overlay)
    return env


def engine_env(
    *, project_root: Path, change_slug: str, change_title: str,
    lint_command: str | None = None,
) -> dict[str, str]:
    """Return the environment variables the engine injects into every subprocess.

    Variables:
        IMP_PROJECT_ROOT  — absolute path of the project root on the host.
        IMP_CHANGE_SLUG   — URL-safe identifier for the change (e.g. "add-oauth").
        IMP_CHANGE_TITLE  — human-readable title for the change.
        IMP_LINT_COMMAND  — optional lint-clean gate command, injected only when
                            configured. A falsy command (None or empty string) is
                            treated as absent => no lint gate, so the var is omitted.
    """
    env = {
        "IMP_PROJECT_ROOT": str(project_root),
        "IMP_CHANGE_SLUG": change_slug,
        "IMP_CHANGE_TITLE": change_title,
    }
    if lint_command:
        env["IMP_LINT_COMMAND"] = lint_command
    return env
