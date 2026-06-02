"""Engine-provided environment variables.

These are the variables the engine guarantees to every subprocess it spawns on
behalf of a Change — script gates (`run_script_gate`) and the agent `bash` tool.
Centralizing them here keeps the two execution paths consistent and gives future
engine vars a single home.

All paths are host paths. There is no sandbox: the `/project/` prefix used in
`fs_permissions` is a virtual namespace only, never a real mount point.
"""

from __future__ import annotations

from pathlib import Path


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
