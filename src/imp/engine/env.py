"""Engine-provided environment variables.

These are the variables the engine guarantees to every subprocess it spawns on
behalf of a quest — script gates (`run_script_gate`) and the agent `bash` tool.
Centralizing them here keeps the two execution paths consistent and gives future
engine vars a single home.

All paths are host paths. `IMP_PROJECT_ROOT` is the absolute worktree path on the
host. Tool paths are worktree-relative.
"""

from __future__ import annotations

import os
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from imp.paths import project_state_dir


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
        """Bundle the four `test_cmd_*` config fields, a `false` command reading as
        no command. Duck-typed on `config` so this stays free of an
        `imp.providers.config` import."""
        return cls(
            fast=config.test_cmd_fast or None,
            slow=config.test_cmd_slow or None,
            fast_timeout=config.test_cmd_fast_timeout,
            slow_timeout=config.test_cmd_slow_timeout,
        )


@dataclass(frozen=True, slots=True)
class Undecided:
    """The stack-dependent settings this project has never decided, read straight
    off Config — the last place `None` (never decided) is distinguishable from
    `False` (decided: needs none). No `test_cmd_slow`: unset already means "fall
    back to test_cmd_fast", so it is never raised. Duck-typed on `config` like
    TestGates.from_config, to stay free of an `imp.providers.config` import."""

    test_cmd_fast: bool = False
    worktree_setup: bool = False
    pythonpath: bool = False

    @classmethod
    def from_config(cls, config: Any) -> Undecided:
        return cls(
            test_cmd_fast=config.test_cmd_fast is None,
            worktree_setup=config.worktree_setup is None,
            pythonpath=config.pythonpath is None,
        )

    def __bool__(self) -> bool:
        return self.test_cmd_fast or self.worktree_setup or self.pythonpath


# The default wherever `undecided` threads: a shared immutable "nothing to raise".
ALL_DECIDED = Undecided()


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
    "IMP_QUEST_ID",
    "IMP_QUEST_TITLE",
    "IMP_QUEST_DIR",
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

    `PYTHONPATH` is special-cased: it is a *prefix* of worktree-relative import
    roots (from `engine_env`), not an authoritative value. Its entries are
    prepended ahead of any inherited `PYTHONPATH` so the worktree's own sources
    win over an editable install that pins a sibling checkout — dropping empty
    inherited entries and any inherited entry that duplicates a prefix entry
    (per-entry dedup, so a multi-root prefix stays clean), and preserving the
    remaining ambient entries in order after the prefix.
    """
    env = {k: v for k, v in parent.items() if k not in ENGINE_ENV_VARS}
    overlay = dict(overlay)
    pythonpath_prefix = overlay.pop("PYTHONPATH", None)
    env.update(overlay)
    if pythonpath_prefix:
        prefix_entries = pythonpath_prefix.split(os.pathsep)
        inherited = parent.get("PYTHONPATH", "")
        # Drop any inherited entry equal to one of the prefix entries (and any
        # empty entry) so a multi-root prefix doesn't grow a redundant copy.
        rest = [p for p in inherited.split(os.pathsep)
                if p and p not in prefix_entries]
        env["PYTHONPATH"] = os.pathsep.join([*prefix_entries, *rest])
    return env


QUEST_TMP_ENV = "IMP_QUEST_TMP"


def quest_scratch_root() -> Path:
    """Host-side provisioning-scratch base from IMP_QUEST_TMP (else system temp).
    Read directly (not via TMPDIR): the session daemon derives its own paths and
    must not repoint TMPDIR."""
    override = os.environ.get(QUEST_TMP_ENV)
    return Path(override) if override else Path(tempfile.gettempdir())


QUEST_VAR_TMP_ENV = "IMP_QUEST_VAR_TMP"


def quest_var_tmp_root() -> Path:
    """Overlay-capable scratch base from IMP_QUEST_VAR_TMP (else /var/tmp); must stay on
    real disk since overlayfs refuses a tmpfs upperdir."""
    override = os.environ.get(QUEST_VAR_TMP_ENV)
    return Path(override) if override else Path("/var/tmp")


def engine_env(
    *, project_root: Path, quest_id: str, quest_title: str,
    quest_dir: Path | None = None,
    control_root: Path | None = None,
    output_path: Path | None = None,
    test_gates: TestGates | None = None,
    pythonpath: tuple[str, ...] = (),
) -> dict[str, str]:
    """Return the environment variables the engine injects into every subprocess.

    Variables:
        IMP_PROJECT_ROOT  — absolute path of the project root on the host.
        IMP_QUEST_ID      — the stable, immutable quest identity; hook authors
                            MUST key on this.
        IMP_QUEST_TITLE   — human-readable title for the quest.
        IMP_QUEST_DIR     — absolute host path of the quest state directory;
                            injected only when quest_dir is provided. Scripts
                            anchor on this instead of a cwd-relative .imp/quests/
                            so the state directory survives worktree relocation.
        IMP_STATE_DIR     — absolute host path of this project's state directory;
                            injected only when control_root is provided. Under the
                            sandbox mask the `repo` marker is unreadable, so an
                            in-sandbox `imp` resolves through this override instead
                            of the slug walk.
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
        PYTHONPATH        — one entry per `pythonpath` root, each resolved against
                            project_root (the worktree for a live quest), joined
                            by os.pathsep in listed order; e.g. `src` =>
                            <worktree>/src, `.` => <worktree>. Empty pythonpath =>
                            no PYTHONPATH key (unchanged when off). Emitted as a
                            bare prefix here; overlay_engine_env prepends it to any
                            inherited PYTHONPATH. The shadow relies on PYTHONPATH
                            preceding site-packages on sys.path — true for
                            path-style editable installs; a strict PEP 660 editable
                            that registers a sys.meta_path finder resolves first
                            and is not shadowable by PYTHONPATH (out of scope).
    """
    env = {
        "IMP_PROJECT_ROOT": str(project_root),
        "IMP_QUEST_ID": quest_id,
        "IMP_QUEST_TITLE": quest_title,
        **HEADLESS_ENV,
    }
    if quest_dir is not None:
        env["IMP_QUEST_DIR"] = str(quest_dir)
    if control_root is not None:
        env["IMP_STATE_DIR"] = str(project_state_dir(control_root))
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
    if pythonpath:
        env["PYTHONPATH"] = os.pathsep.join(
            str(project_root / entry) for entry in pythonpath)
    return env
