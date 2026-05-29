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


def engine_env(*, project_root: Path, artifacts_dir: Path) -> dict[str, str]:
    return {
        "IMP_PROJECT_ROOT": str(project_root),
        "IMP_ARTIFACTS_DIR": str(artifacts_dir),
    }
