"""Engine-provided environment variables.

These are the variables the engine guarantees to every subprocess it spawns on
behalf of a Change — script gates (`run_script_gate`) and the agent `bash` tool.
Centralizing them here keeps the two execution paths consistent and gives future
engine vars a single home.

All paths are host paths. `IMP_PROJECT_ROOT` is the absolute worktree path on the
host. Tool paths are worktree-relative.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True, slots=True)
class TestGates:
    """Resolved tiered gate commands + per-tier timeouts, threaded to every gate
    subprocess. A falsy command (None or "") => its IMP_TEST_CMD_* var is omitted
    (treated as not configured); a None timeout => its var is omitted and the gate
    script applies its own default."""

    __test__ = False  # pytest: not a test class — these are test-command gates
    fast: str | None = None
    slow: str | None = None
    fast_timeout: int | None = None
    slow_timeout: int | None = None

    @classmethod
    def from_config(cls, config: Any) -> TestGates:
        """Bundle the four `test_cmd_*` config fields. Duck-typed on `config` so
        this stays free of an `imp.providers.config` import."""
        return cls(
            fast=config.test_cmd_fast,
            slow=config.test_cmd_slow,
            fast_timeout=config.test_cmd_fast_timeout,
            slow_timeout=config.test_cmd_slow_timeout,
        )


# Headless overrides forced into every engine-spawned subprocess so no git/tool
# invocation blocks on an interactive child. GIT_EDITOR beats core.editor and
# $VISUAL/$EDITOR and accepts the prepared message file unchanged (exit 0);
# GIT_TERMINAL_PROMPT=0 makes git fail fast instead of reading /dev/tty; EDITOR
# and VISUAL cover non-git tools. Emitted unconditionally by engine_env and, via
# ENGINE_ENV_VARS membership, stripped-then-overridden by overlay_engine_env so a
# user's ambient EDITOR=vim never reaches an agent child. The keys are also the
# sandbox allowlist's non-IMP_ headless exception (imp.engine.sandbox).
HEADLESS_ENV = {
    "GIT_EDITOR": "true",
    "GIT_TERMINAL_PROMPT": "0",
    "EDITOR": "true",
    "VISUAL": "true",
}

# Every env var this module manages. `engine_env` is the AUTHORITY for these: a
# subprocess must see exactly what `engine_env` produces, never an inherited copy.
# `IMP_TEST_CMD_FAST` is conditionally present (absent => no fast gate), so
# without explicit stripping a stale ambient value (e.g. from a parent imp process
# during imp-on-imp dogfooding) would leak through a `{**os.environ, ...}` merge
# and falsely enable the gate. Use `overlay_engine_env` to merge, not a raw dict spread.
ENGINE_ENV_VARS = frozenset({
    "IMP_PROJECT_ROOT",
    "IMP_CHANGE_ID",
    "IMP_CHANGE_TITLE",
    "IMP_CHANGE_DIR",
    "IMP_OUTPUT",
    "IMP_TEST_CMD_FAST",
    "IMP_TEST_CMD_SLOW",
    "IMP_TEST_CMD_FAST_TIMEOUT",
    "IMP_TEST_CMD_SLOW_TIMEOUT",
    *HEADLESS_ENV,
})


def overlay_engine_env(
    parent: Mapping[str, str], overlay: Mapping[str, str],
) -> dict[str, str]:
    """Merge `overlay` (an `engine_env` result) onto `parent`, authoritatively.

    Any engine-managed var inherited from `parent` is dropped first, so only the
    vars `overlay` actually provides reach the subprocess. This prevents a stale
    ambient engine var — notably `IMP_TEST_CMD_FAST` from an outer imp process —
    from leaking past `engine_env`'s "absent => no fast gate" contract.

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
    *, project_root: Path, quest_id: str, change_title: str,
    change_dir: Path | None = None,
    output_path: Path | None = None,
    test_gates: TestGates | None = None,
) -> dict[str, str]:
    """Return the environment variables the engine injects into every subprocess.

    Variables:
        IMP_PROJECT_ROOT  — absolute path of the project root on the host.
        IMP_CHANGE_ID     — the stable, immutable Change identity; hook authors
                            MUST key on this.
        IMP_CHANGE_TITLE  — human-readable title for the change.
        IMP_CHANGE_DIR    — absolute host path of the change state directory;
                            injected only when change_dir is provided. Scripts
                            anchor on this instead of a cwd-relative .imp/change/
                            so the state directory survives worktree relocation.
        IMP_OUTPUT        — path to the JSON file where a script writes its declared
                            typed output: fields; injected only when output_path is
                            provided (i.e. when an output: schema is in play).
        IMP_TEST_CMD_FAST / IMP_TEST_CMD_SLOW — tiered gate commands (cheap fast
                            gate vs. complete slow gate). Each injected only when
                            its TestGates field is truthy; a falsy value => absent.
        IMP_TEST_CMD_FAST_TIMEOUT / IMP_TEST_CMD_SLOW_TIMEOUT — per-tier wall-clock
                            ceilings (seconds). Injected only when set; absent =>
                            run-verify.sh applies its own per-tier default (120/300).
                            IMP_-prefixed, so the sandbox allowlist passes them
                            through with no edit.
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
        "IMP_CHANGE_ID": quest_id,
        "IMP_CHANGE_TITLE": change_title,
        **HEADLESS_ENV,
    }
    if change_dir is not None:
        env["IMP_CHANGE_DIR"] = str(change_dir)
    if output_path is not None:
        env["IMP_OUTPUT"] = str(output_path)
    if test_gates is not None:
        if test_gates.fast:
            env["IMP_TEST_CMD_FAST"] = test_gates.fast
        if test_gates.slow:
            env["IMP_TEST_CMD_SLOW"] = test_gates.slow
        if test_gates.fast_timeout is not None:
            env["IMP_TEST_CMD_FAST_TIMEOUT"] = str(test_gates.fast_timeout)
        if test_gates.slow_timeout is not None:
            env["IMP_TEST_CMD_SLOW_TIMEOUT"] = str(test_gates.slow_timeout)
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
