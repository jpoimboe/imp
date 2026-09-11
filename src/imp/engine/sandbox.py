# SPDX-License-Identifier: MIT
# Copyright 2026 Josh Poimboeuf
"""Shell sandbox (bubblewrap) — pure policy core + spawn builder (spec Part B).

Two spawn points consume this: the `bash` tool (agents/tools/bash.py) and script
gates (engine/gates.py). `resolve_policy` does the one-time stat I/O and bakes a
frozen `SandboxPolicy`; `build_sandbox_spawn` is pure given that policy.

The flag ORDER in build_sandbox_spawn is load-bearing: bwrap is last-wins, so the
writable-home bind (when a home_layer is present) and then the per-quest scratch
bind are emitted EARLY (right after the ro-floor, before worktree/git/remask) and
the secret masks MUST be emitted AFTER every bind (incl. extra_writable), else
a config-driven bind could re-expose a masked secret dir (spec L1-r5). Host /tmp
has no covering bind of its own: it stays on the --ro-bind / / floor, readable but
not writable, and only the per-quest scratch dir is re-exposed rw at its real path.

`--unshare-pid` isolates the process table (a shell cannot see or signal
anything outside its own namespace). One keeper namespace per agent run holds
every command that run issues, so processes persist across calls while killpg
still reaches a call's own group. Teardown is SIGKILL to the keeper's ns-init,
whose pid `--info-fd` reports: killing pid 1 of a pid namespace makes the kernel
kill everything inside, so setsid / double-fork / re-grouping stop mattering.
`--die-with-parent` goes on the KEEPER — the dead-man's switch for imp dying
abnormally, safe there because the keeper is never in an interrupted process
group. It must NOT go on the per-call join, where it would collapse the SIGTERM
grace (spec B.9). No `--new-session` (F-8).

The command travels in `$IMP_COMMAND` and the cwd in `$IMP_CWD`, off argv, so an
agent's `pkill -f` cannot match the shell running it; both are unset before
`eval`, so children see the same environment as before. Not secrecy —
`/proc/<pid>/environ` and `ps e` inside the sandbox still show it. The cwd must
be set by an inner `cd`, never `--wd` or Popen's `cwd=`: both resolve in the
caller's mount namespace and would hand the shell the host's view of a masked
path.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import platform
import random
import select
import shlex
import shutil
import signal
import struct
import subprocess
import tempfile
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from functools import lru_cache
from pathlib import Path
from typing import Any, overload

from imp.engine.env import (
    HEADLESS_ENV,
    ensure_quest_tmp_dir,
    merge_pythonpath_prefix,
    overlay_engine_env,
    quest_var_tmp_root,
)
from imp.engine.shim import imp_shim_dir
from imp.paths import imp_home, project_state_dir
from imp.remote.session.discover import session_socket_path


@dataclass(frozen=True, slots=True)
class HomeLayer:
    """The writable-home layer bound over $HOME for an active-sandbox agent.
    Fallback (rw) shape: only `home` set → a plain `--bind home home`. COW overlay
    shape: merged/upper/work set → bwrap is wrapped with unshare + mount -t
    overlay (lowerdir=home) and the merged view is bound onto `home`; userxattr
    records the mount shape overlay_mode negotiated for this host."""
    home: str
    merged: str = ""     # overlay mountpoint bound onto $HOME ("" → plain rw bind of home)
    upper: str = ""      # overlay upperdir (captured writes; discarded at teardown)
    work: str = ""       # overlay workdir
    userxattr: bool = False  # mount -o userxattr (opaque marker in user.overlay.*)

    @property
    def is_overlay(self) -> bool:
        return bool(self.upper)


@dataclass(frozen=True, slots=True)
class OverlayRoot:
    """The dir a keeper namespace mints its home-overlay layer under, paired with
    the mount shape overlay_mode negotiated for this host. Pairing them keeps a
    minted layer's userxattr flag inseparable from the root it came from, so a
    layer claiming a shape the probe never validated cannot be constructed."""
    path: str
    userxattr: bool


@dataclass(frozen=True, slots=True)
class SandboxPolicy:
    active: bool                              # True → wrap with bwrap; False → bare sh -c
    worktree: str                             # rw bind at REAL path
    git_common_dir: str                       # <control>/.git rw bind
    git_ro_remask: tuple[str, ...]  # .git: hooks, config, refs/heads, refs/stash, index (ro)
    writable_caches: tuple[str, ...]          # rw toolchain cache dirs (existing only)
    extra_readable: tuple[str, ...]           # config extra_readable (ro)
    extra_writable: tuple[str, ...]           # config extra_writable (rw)
    secret_dirs: tuple[str, ...]              # --tmpfs masks (existing only) incl. /run/user/$UID
    secret_files: tuple[str, ...]             # --ro-bind /dev/null masks (existing only)
    child_env: tuple[tuple[str, str], ...]     # resolved child environment key/value pairs
    network: bool                             # True → no --unshare-net
    note: str = "" # "" | "unsandboxed_by_config" | "sandbox_disabled" | "unsandboxed_override"
    quest_home: str = ""                     # <state dir>/quests/<id> rw bind ("" → none)
    state_mask: str = ""                     # fail-closed tmpfs mask ("" → none)
    projects_ro: str = ""                    # ro bind of imp_home()/projects ("" → none)
    state_repo_ro: str = ""                  # ro re-expose of <state dir>/repo ("" → none)
    readonly_overlays: tuple[str, ...] = ()  # ro subtrees; after git_ro_remask, before caches
    git_ref_reexpose: tuple[str, ...] = ()   # rw re-expose of refs/heads/imp; after git_ro_remask
    disposable_tree: bool = False            # agent runs in a disposable git worktree
    home: str = ""                           # real $HOME ("" → inactive/unresolved)
    home_layer: HomeLayer | None = None      # writable-home layer (None → ro read-through)
    overlay_root: OverlayRoot | None = None  # per-step root keepers mint layers under
    # per-quest scratch dir, rw at its own real path ("" → no bind; host /tmp fully ro)
    quest_tmp_dir: str = ""
    quest_var_tmp_dir: str = ""  # per-quest dir onto /var/tmp ("" → no bind; host ro)
    git_worktrees_mask: str = ""  # <control>/.git/worktrees tmpfs mask ("" → none)
    git_worktree_self: str = ""   # rw re-expose of .git/worktrees/<self> ("" → none)

    def bind_plan(self) -> list[tuple[str, str]]:
        """The ordered path-covering binds, in bwrap EMIT ORDER — the SINGLE
        source of truth shared by build_sandbox_spawn (which formats each entry to
        argv), allows_write, and allows_read (both evaluate last-covering-wins).
        kind is one of rw / ro / mask_dir / mask_file; rw grants write. The order is
        load-bearing: the ro overlays (git_ro_remask, readonly_overlays, extra_readable)
        are NOT globally last-wins — a later writable bind re-exposes a path they
        cover — only the secret masks are unconditionally last. Add a new bind here
        and ALL consumers pick it up; the argv-parity test enforces no drift. The
        home_layer rw bind goes FIRST (so caches/worktree/secrets over $HOME win),
        then the per-quest scratch bind — quest_tmp_dir re-exposed rw at its own real
        path, the dir TMPDIR points at. Host /tmp gets no covering bind and stays on
        the read-only floor. Then the per-quest read-write /var/tmp scratch bind
        (emitted only when quest_var_tmp_dir is set; empty → no /var/tmp bind, host
        /var/tmp stays on the read-only floor), and the secret masks unconditionally LAST.

        The state tmpfs (state_mask) is a fail-closed mask over THIS project's
        state directory, preceded in the same state_block by the read-only
        projects_ro bind exposing imp's records of OTHER projects (without it the
        home_layer rw bind over $HOME would leave them writable). Both sit INSIDE
        worktree_block: a draft worktree is the control root, which may be an
        ANCESTOR of imp_home()/projects, so an ro bind emitted before it would lose
        last-wins to the worktree's rw bind and expose other projects WRITABLE. The
        block's position is worktree-relative and LOAD-BEARING. Normal
        quest: the worktree is nested at <state>/worktrees/<id> (a DESCENDANT of the
        mask), so the tmpfs precedes the worktree rw bind and last-wins re-exposes the
        nested worktree on top. Draft/degenerate agent: worktree == control_root (which
        is NOT under the mask), so the control-root rw bind precedes the tmpfs and the
        worktree is deliberately NOT re-bound after it. In BOTH cases the quest_home
        rebind and the own steps/events masks come AFTER the tmpfs. Invariant: after the
        whole plan applies, the exposed set under the mask is EXACTLY the current
        quest's own home (minus its own steps/events), the read-only `repo` ownership
        marker (state_repo_ro, so the $IMP_STATE_DIR guard can still read it), plus the
        current worktree iff the worktree is a descendant of the mask; everything else
        under it is masked.
        Mis-ordering buries a live quest's own bookkeeping (normal) or leaks every
        sibling (draft).

        The .git/worktrees mask (git_worktrees_mask) + self re-expose
        (git_worktree_self) are emitted AFTER git_ref_reexpose, the same last-wins
        shape as state_mask: the tmpfs hides every sibling admin dir under
        .git/worktrees, then the self re-expose (emitted only for a materialized
        worktree) re-binds the current quest's own .git/worktrees/<self> on top so
        its HEAD/index/logs stay writable."""
        state_block = [
            *([(self.projects_ro, "ro")] if self.projects_ro else []),
            *([(self.state_mask, "mask_dir")] if self.state_mask else []),
            *([(self.state_repo_ro, "ro")] if self.state_repo_ro else []),
        ]
        nested = bool(self.state_mask) and Path(self.worktree).is_relative_to(
            Path(self.state_mask))
        worktree_block = (
            [*state_block, (self.worktree, "rw")] if nested
            # draft: worktree is OUTSIDE the mask, so bind it BEFORE state_block
            else [(self.worktree, "rw"), *state_block])
        return [
            *([(self.home_layer.home, "rw")] if self.home_layer is not None else []),
            *([(self.quest_tmp_dir, "rw")] if self.quest_tmp_dir else []),
            *([("/var/tmp", "rw")] if self.quest_var_tmp_dir else []),
            *worktree_block,
            (self.git_common_dir, "rw"),
            *([(self.quest_home, "rw")] if self.quest_home else []),
            *[(p, "ro") for p in self.git_ro_remask],
            *[(p, "rw") for p in self.git_ref_reexpose],
            *([(self.git_worktrees_mask, "mask_dir")] if self.git_worktrees_mask else []),
            *([(self.git_worktree_self, "rw")] if self.git_worktree_self else []),
            *[(p, "ro") for p in self.readonly_overlays],
            *[(c, "rw") for c in self.writable_caches],
            *[(r, "ro") for r in self.extra_readable],
            *[(w, "rw") for w in self.extra_writable],
            *[(d, "mask_dir") for d in self.secret_dirs],
            *[(f, "mask_file") for f in self.secret_files],
        ]

    def allows_write(self, path: Path) -> bool:
        """In-process mirror of the sandbox's write exposure. Walks bind_plan()
        in emit order; the LAST bind covering `path` decides. Writable iff that
        last bind is rw; a read-only overlay (git_ro_remask, extra_readable), a
        secret mask, or no covering bind denies."""
        verdict: str | None = None
        for bind, kind in self.bind_plan():
            if path.is_relative_to(Path(bind)):
                verdict = kind
        return verdict == "rw"

    def allows_read(self, path: Path) -> bool:
        """In-process mirror of the sandbox's read exposure. Walks bind_plan() in
        emit order (the shared source of truth with allows_write); the LAST bind
        covering `path` decides: readable unless that last bind is a mask
        (mask_dir/mask_file). An uncovered path is readable — the host --ro-bind / /
        floor. Deliberately stricter than bwrap (which serves an empty for a masked
        read); see spec Decisions."""
        verdict: str | None = None
        for bind, kind in self.bind_plan():
            if path.is_relative_to(Path(bind)):
                verdict = kind
        return verdict not in ("mask_dir", "mask_file")


def _is_engine_passthrough(key: str) -> bool:
    """Engine-env keys allowed through the sandbox env allowlist: every IMP_*
    var, PYTHONPATH (the imp-on-imp import shadow), and the four headless
    overrides (GIT_EDITOR / GIT_TERMINAL_PROMPT / EDITOR / VISUAL) that keep a
    sandboxed git/tool invocation non-interactive. Shared by the per-spawn overlay
    (_spawn_child_env, used by build_sandbox_spawn and SandboxNamespace.join) and
    the policy bake in _build_child_env, so they cannot drift."""
    return key.startswith("IMP_") or key == "PYTHONPATH" or key in HEADLESS_ENV


SANDBOX_ENTERED_MARKER = "__IMP_SANDBOX_ENTERED__"
SANDBOX_SPAWN_FAILED_MESSAGE = (
    "the OS sandbox could not start your command; it did NOT run "
    "(a sandbox-setup failure, not your command's own exit)"
)


def sandbox_spawn_failed(stderr: str, *, sandbox_active: bool) -> bool:
    """True iff sandbox was active but the entry sentinel is absent (command never ran)."""
    return sandbox_active and SANDBOX_ENTERED_MARKER not in stderr


@overload
def normalize_returncode(returncode: int, *, imp_killed: bool) -> int: ...
@overload
def normalize_returncode(returncode: None, *, imp_killed: bool) -> None: ...
def normalize_returncode(returncode: int | None, *, imp_killed: bool) -> int | None:
    """Spec §7: a negative returncode means IMP killed it; anything the command did
    to itself reports 128+n. nsenter re-raises a signal on itself when its direct
    child dies by one, where bwrap mapped it to 128+n, so without this a command's
    own `kill -9 $$` would read as imp having killed the agent's tool."""
    if returncode is not None and returncode < 0 and not imp_killed:
        return 128 - returncode
    return returncode


_SANDBOX_DETAIL_MAX_CHARS = 200    # one-line diagnosis budget in a runner_warning payload


def sandbox_failure_detail(stderr: str) -> str:
    """The sandbox's own first non-empty stderr line, clipped — "" when it said nothing."""
    for line in stderr.splitlines():
        if line.strip():
            return line.strip()[:_SANDBOX_DETAIL_MAX_CHARS]
    return ""


SANDBOX_SPAWN_MAX_ATTEMPTS = 5   # 1 initial + 4 retries
_SANDBOX_SPAWN_BACKOFF_BASE_S = 0.05
_SANDBOX_SPAWN_BACKOFF_CAP_S = 0.4


def sandbox_spawn_backoff_delay(attempt: int) -> float:
    """Full-jitter exponential backoff (seconds) BEFORE retry `attempt` (1-based):
    a uniform sample in [0, min(cap, base * 2**(attempt-1))]. base 50ms doubling
    to a 400ms cap. Retry is provably safe (the inner command never ran), so full
    jitter — not equal jitter — spreads a thundering herd of concurrent gates."""
    ceiling = min(_SANDBOX_SPAWN_BACKOFF_CAP_S,
                  _SANDBOX_SPAWN_BACKOFF_BASE_S * (2 ** (attempt - 1)))
    return random.uniform(0.0, ceiling)


KEEPER_PAYLOAD = "exec sleep infinity"   # the keeper's own command; it holds the namespaces open


@dataclass(frozen=True, slots=True)
class SandboxSpawn:
    """One spawn's argv AND the environment to spawn it with. Produced by a single
    call so the two cannot be mismatched: the argv carries no environment flags, so
    the env field is the sole source of truth — pairing them prevents a caller from
    accidentally supplying os.environ and dumping the user's whole environment into
    the sandbox. `pass_fds` are the fds the child must inherit for its argv to mean
    anything (the keeper's --info-fd, a join's pinned namespace fds); they ride with
    the argv for the same reason, and are () for an ordinary spawn."""
    argv: list[str]
    env: dict[str, str]
    pass_fds: tuple[int, ...] = ()


def _spawn_child_env(policy: SandboxPolicy,
                     engine_env: Mapping[str, str]) -> dict[str, str]:
    """The child environment for ONE spawn: policy.child_env overlaid with the
    engine-passthrough slice of the LIVE engine_env, which authoritatively
    supersedes the values frozen into the policy at run start (name-on-intent: a
    post-rename IMP_QUEST_TITLE must beat the empty one baked at run start). Dict
    preserves first-seen order; reassigning a key updates it in place."""
    env = dict(policy.child_env)
    for k, v in engine_env.items():
        if _is_engine_passthrough(k):
            env[k] = v
    return env


_BARE_SCRIPT = (          # no `cd`: one mount namespace, so the spawn's own cwd= applies
    'cmd=$IMP_COMMAND\n'
    'unset IMP_COMMAND\n'
    'eval "$cmd"\n'
)


def build_sandbox_spawn(
    command: str, *, cwd: str, policy: SandboxPolicy | None,
    engine_env: Mapping[str, str], environ: Mapping[str, str],
    shim_dir: str = "", keeper_info_fd: int | None = None,
) -> SandboxSpawn:
    """Return the argv to spawn `command` AND the environment to spawn it with.
    When the policy is active, wrap with bwrap per spec §B.2 (fixed flag order) and
    the env is the resolved child environment; otherwise a bare `sh -c` evaluates
    the command out of `$IMP_COMMAND` — off argv like a join's, so `/sandbox off`
    does not keep the self-`pkill` footgun — with the host environment (`environ`
    overlaid with `engine_env`, plus the `shim_dir` PATH prefix). A None policy is
    the same unsandboxed case. `shim_dir` is ignored under an active
    policy: the PATH shim is already baked into `policy.child_env` by `_build_child_env`.

    `engine_env` is the LIVE, per-spawn engine_env (an `engine_env()` result) and
    is the single source of truth for the sandbox's `IMP_*` vars: every IMP_*-
    prefixed key it carries enters the returned child environment, authoritatively
    superseding whatever was baked into `policy.child_env` at run start. The policy
    is resolved once per run (the stat-once design), but name-on-intent makes
    IMP_QUEST_TITLE mutate mid-run on a rename — so a gate or tool spawned after
    the rename MUST see the live values, not the empty ones frozen at run start.

    Required (not optional) on purpose: a sandboxed spawn without its live engine
    env is always a bug, so every call site — present and future — is forced to
    supply it rather than silently inheriting a stale snapshot. The allowlist is
    the IMP_ prefix plus PYTHONPATH (`_is_engine_passthrough`): any new IMP_* var
    flows through with no edit here, and PYTHONPATH passes through as the single
    deliberate non-IMP_ exception — the imp-on-imp import shadow — matching
    `_build_child_env`.

    `keeper_info_fd` makes this a KEEPER spawn: bwrap additionally gets
    `--info-fd <fd>` (where it reports the ns-init's pid) and `--die-with-parent`
    (the dead-man's switch for imp dying abnormally), and the fd comes back on
    `SandboxSpawn.pass_fds` so the flag and the inherited fd cannot be separated.
    Both flags belong to the keeper and only the keeper: on a per-call spawn
    `--die-with-parent` would collapse the runner's SIGTERM grace (spec B.9).
    `SandboxNamespace.open()` is the only caller and passes KEEPER_PAYLOAD.

    An ACTIVE policy carrying overlay_root with an un-minted home layer is a
    fail-open bug (it would bind the real $HOME rw) and raises; the INACTIVE
    variant of that same shape is the reachable `/sandbox off` case and is fine."""
    if policy is None or not policy.active:
        env = overlay_engine_env(environ, engine_env)
        if shim_dir:
            env["PATH"] = shim_dir + os.pathsep + env.get("PATH", "")
        env["IMP_COMMAND"] = command
        return SandboxSpawn(argv=["sh", "-c", _BARE_SCRIPT], env=env)

    if (policy.overlay_root is not None and policy.home_layer is not None
            and not policy.home_layer.is_overlay):
        # Fail CLOSED: this policy skipped prepare_spawn_policy, so the rw home
        # bind would expose the REAL $HOME instead of an overlay merged view.
        raise AssertionError(
            f"active policy carries overlay_root {policy.overlay_root!r} with an "
            f"un-minted home layer; spawn through prepare_spawn_policy")

    argv: list[str] = ["bwrap"]
    # 1. whole host readable
    argv += ["--ro-bind", "/", "/"]
    # 2-10. path-covering binds, in emit order from policy.bind_plan() (the single
    #    source shared with allows_write). Emit order is load-bearing and last-wins:
    #    the home_layer rw bind (when present) goes FIRST so caches/worktree/secrets
    #    over $HOME win; the per-quest scratch bind goes next, at its own real path,
    #    before the worktree/git binds — host /tmp itself keeps no bind and stays on
    #    the read-only floor; secret masks go LAST.
    for bind, kind in policy.bind_plan():
        if kind == "rw":
            hl = policy.home_layer
            if hl is not None and hl.is_overlay and bind == hl.home:
                src = hl.merged                       # overlay merged view onto $HOME
            elif bind == "/var/tmp" and policy.quest_var_tmp_dir:
                src = policy.quest_var_tmp_dir         # per-quest host dir onto namespace /var/tmp
            else:
                src = bind                            # bound at its own real path
            argv += ["--bind", src, bind]
        elif kind == "ro":
            argv += ["--ro-bind", bind, bind]
        elif kind == "mask_dir":
            argv += ["--tmpfs", bind]
        elif kind == "mask_file":
            argv += ["--ro-bind", "/dev/null", bind]
        else:  # unreachable; guards a future bind_plan kind from silently vanishing
            raise AssertionError(f"unknown bind kind {kind!r} for {bind!r}")
    # 11. proc/dev
    argv += ["--proc", "/proc", "--dev", "/dev"]
    # 12. chdir
    argv += ["--chdir", cwd]
    # 12a. KEEPER only: bwrap reports the ns-init's pid on this fd, and
    # --die-with-parent tears the whole namespace down if imp dies abnormally.
    if keeper_info_fd is not None:
        argv += ["--info-fd", str(keeper_info_fd), "--die-with-parent"]
    # 13. the child environment — delivered out-of-band on the spawn, NOT on argv
    # (argv is world-readable via /proc). The IMP_* slice comes authoritatively
    # from the live per-spawn engine_env (prefix allowlist) so a post-rename title
    # wins over the value frozen into policy.child_env at run start
    # (name-on-intent). Dict preserves first-seen order; reassigning a key updates
    # in place.
    child_env = _spawn_child_env(policy, engine_env)
    # 14. namespaces — a PER-CALL spawn carries no --die-with-parent / --new-session
    # (B.9); --unshare-pid is safe (the group kill crosses the namespace)
    argv += ["--unshare-user", "--unshare-ipc", "--unshare-uts", "--unshare-pid"]
    # 15. optional network isolation
    if not policy.network:
        argv += ["--unshare-net"]
    # 16. the command, prefixed with the entry sentinel. The outer sh parses only
    # this fixed script, then execs an inner sh that parses the command — passed as
    # an ARGUMENT ($0), never interpolated, so a syntax error there cannot suppress
    # the marker. exec adds no process, so pgid == pid holds for the group kill.
    argv += ["sh", "-c",
             f"printf '%s\\n' {shlex.quote(SANDBOX_ENTERED_MARKER)} >&2; "
             'exec sh -c "$0"',
             command]
    pass_fds = () if keeper_info_fd is None else (keeper_info_fd,)
    hl = policy.home_layer
    if hl is not None and hl.is_overlay:
        # Fail loud on mount failure: exit nonzero BEFORE the exec, so the inner
        # command (and its sentinel) never runs — sentinel ABSENCE is the sole
        # signal, so no bespoke marker/code is needed.
        opt = ",userxattr" if hl.userxattr else ""
        mount = (
            f"mount -t overlay overlay -o "
            f"lowerdir={shlex.quote(hl.home)},"
            f"upperdir={shlex.quote(hl.upper)},"
            f"workdir={shlex.quote(hl.work)}{opt} {shlex.quote(hl.merged)} "
            f"|| exit 1; "
            f"exec {shlex.join(argv)}"
        )
        return SandboxSpawn(
            argv=["unshare", "--user", "--map-root-user", "--mount", "sh", "-c", mount],
            env=child_env, pass_fds=pass_fds)
    return SandboxSpawn(argv=argv, env=child_env, pass_fds=pass_fds)


_NSENTER_FLAG = {"user": "user", "mnt": "mount", "pid": "pid", "uts": "uts",
                 "ipc": "ipc", "net": "net"}
_KEEPER_NS_KINDS = ("user", "mnt", "pid", "uts", "ipc")   # + net iff policy isolates it

_NS_GET_USERNS = 0xB701   # ioctl(ns_fd) -> fd on the user namespace that OWNS it

# Python's termios exports neither: the devpts slave-node unlock and the pty number.
TIOCSPTLCK = 0x40045431
TIOCGPTN = 0x80045430

_JOIN_SCRIPT = (
    'cd "$IMP_CWD" || exit 1\n'
    'cmd=$IMP_COMMAND\n'
    'unset IMP_COMMAND IMP_CWD\n'
    'eval "$cmd"\n'
)


_KEEPER_READY_TIMEOUT_S = 30.0   # generous vs the measured 7-15ms spawn-to-ready
_KEEPER_REAP_TIMEOUT_S = 10.0


class SandboxOpenFailed(Exception):
    """The keeper namespace could not be opened, so no command was spawned."""

    def __init__(self, detail: str) -> None:
        super().__init__(detail or "the sandbox namespace could not be opened")
        self.detail = detail


def _read_chunk(fd: int, deadline: float) -> bytes:
    """One read from `fd`, bounded by `deadline`; b"" on EOF or timeout."""
    timeout = deadline - time.monotonic()
    if timeout <= 0:
        return b""
    ready, _, _ = select.select([fd], [], [], timeout)
    return os.read(fd, 65536) if ready else b""


def _read_to_eof(fd: int, deadline: float) -> str:
    """Read `fd` to EOF: bwrap emits the --info-fd JSON in three separate write()
    calls, so parsing the first chunk is wrong. bwrap closes the fd once it has
    written, well before the payload runs, so this cannot wedge on a live keeper."""
    buf = b""
    while chunk := _read_chunk(fd, deadline):
        buf += chunk
    return buf.decode("utf-8", "replace")


def _await_sentinel(fd: int, deadline: float) -> str:
    """Read the keeper's stderr until the entry sentinel arrives, the pipe hits
    EOF, or the deadline passes, and return everything read. Load-bearing, not
    hygiene: bwrap publishes its --info-fd child-pid BEFORE the mount namespace is
    finished, and joining on the pid alone hit execvp ENOENT 29 times in 40 (unsafe
    window 10-20ms) where gating on the sentinel failed 0 in 40. The overlay mount
    runs before the exec and fails loud, so sentinel ABSENCE is the sole failure
    signal — no bespoke marker or exit code is needed."""
    buf = b""
    while SANDBOX_ENTERED_MARKER not in buf.decode("utf-8", "replace"):
        chunk = _read_chunk(fd, deadline)
        if not chunk:
            break
        buf += chunk
    return buf.decode("utf-8", "replace")


def _abandon_keeper(proc: subprocess.Popen[bytes], ns_fds: list[tuple[str, int]],
                    err_fd: int, devpts_fd: int | None) -> None:
    """Tear down a keeper that never became usable, closing everything a live
    namespace would have owned. SIGTERM first: bwrap's monitor handles it and takes
    the sandbox with it, where a SIGKILL to the monitor would leave the ns-init
    alive and the namespace still joinable."""
    for _kind, fd in ns_fds:
        os.close(fd)
    if devpts_fd is not None:
        os.close(devpts_fd)
    os.close(err_fd)
    proc.terminate()
    try:
        proc.wait(timeout=_KEEPER_REAP_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()


def _ns_owner_fd(fds: dict[str, int]) -> int | None:
    """An fd on the user namespace that OWNS the keeper's non-user namespaces, or
    None when that is the payload's own user namespace.

    bwrap nests a SECOND user namespace for the payload whenever the sandbox uid
    differs from the uid it needed in the first one (--dev forces uid 0 there for
    devpts), i.e. whenever imp does not run as uid 0. Capabilities gained by
    entering that child namespace do not reach namespaces owned by its parent, so
    a join that enters it first can never setns into the ipc/uts/pid/mnt/net
    namespaces: the owner namespace has to be entered first."""
    def ident(fd: int) -> tuple[int, int]:
        st = os.fstat(fd)
        return (st.st_dev, st.st_ino)

    owner = fcntl.ioctl(fds["ipc"], _NS_GET_USERNS, 0)
    if ident(owner) == ident(fds["user"]):
        os.close(owner)
        return None
    return owner


@dataclass(slots=True)
class SandboxNamespace:
    """One keeper namespace: a bwrap sandbox whose payload is `sleep infinity`,
    held open so every command joins it instead of building its own. Processes
    persist and stay killable across joins, and close() ends all of them at once.

    `policy` is the PREPARED policy the keeper argv was built from (its home
    overlay layer is minted once, here, not per join). `base_policy` is the value
    open() was handed — the namespace is a frozen picture of that bind plan, so it
    stays valid exactly as long as that value does.

    `ns_fds` pins /proc/<ns-init>/ns/* as open fds and the join names them as
    /proc/self/fd/N rather than `--target <pid>`: no pid-reuse window, and it fails
    CLOSED once the ns-init dies (`nsenter: fork failed: Cannot allocate memory`),
    where a recycled --target pid would fail open.

    `devpts_fd` pins the keeper's OWN /dev/pts the same way, and for the same
    reason — but deliberately outside `ns_fds`, which is spread onto nsenter's
    --<kind>= argv and into pass_fds, where this fd belongs in neither."""
    policy: SandboxPolicy
    base_policy: SandboxPolicy
    ns_fds: tuple[tuple[str, int], ...]
    outer_user_fd: int | None
    devpts_fd: int       # O_PATH dirfd on the keeper's own /dev/pts
    ns_init_pid: int     # pid 1 of the keeper's pid namespace, as seen from imp
    err_fd: int          # read end of the keeper's stderr; the keeper holds the write end for life
    proc: subprocess.Popen[bytes]
    closed: bool = False

    def join(self, command: str, *, cwd: str,
             engine_env: Mapping[str, str]) -> SandboxSpawn:
        """The argv and env to run ONE command inside this namespace.

        The command travels in $IMP_COMMAND and the cwd in $IMP_CWD, off argv, so
        an agent's `pkill -f` cannot match the shell running it; both are unset
        before the eval, so children see the same environment as before. This is
        NOT secrecy — /proc/<pid>/environ and `ps e` inside the sandbox still show
        the command. The cwd must be set by the inner `cd`, never --wd or Popen's
        cwd=: both resolve in the CALLER's mount namespace and would hand the shell
        the host's view of a masked path. The env is rebuilt on every call, never
        captured at open(), so a mid-run IMP_* change reaches the next command.

        The command runs in a CHILD user namespace: entering the keeper's own
        hands it CAP_FULL_SET there whenever bwrap built a single userns level
        (the production shape — the home overlay runs bwrap as root), which is
        enough to `umount` a secret_dirs mask and read what it hid. The child
        namespace owns no mount, and --map-current-user keeps the uid identical,
        so nothing else changes; with two levels the caps are already empty and
        the stage is a no-op."""
        if self.closed:
            raise AssertionError("join on a closed SandboxNamespace")
        argv: list[str] = []
        pass_fds = tuple(fd for _kind, fd in self.ns_fds)
        if self.outer_user_fd is not None:
            argv += ["nsenter", f"--user=/proc/self/fd/{self.outer_user_fd}",
                     "--preserve-credentials", "--"]
            pass_fds += (self.outer_user_fd,)
        argv += ["nsenter"]
        argv += [f"--{_NSENTER_FLAG[kind]}=/proc/self/fd/{fd}" for kind, fd in self.ns_fds]
        argv += ["--preserve-credentials", "--",
                 "unshare", "--user", "--map-current-user", "--",
                 "sh", "-c", _JOIN_SCRIPT]
        env = _spawn_child_env(self.policy, engine_env)
        env["IMP_COMMAND"] = command
        env["IMP_CWD"] = cwd
        return SandboxSpawn(argv=argv, env=env, pass_fds=pass_fds)

    def join_exec(self, argv: Sequence[str], *, cwd: str,
                  engine_env: Mapping[str, str]) -> SandboxSpawn:
        """The argv and env to EXEC one program inside this namespace — `join`'s
        shape for an argv payload instead of a shell command.

        Two deliberate differences, both forced by the payload's shape. The argv
        is exec'd DIRECTLY, with no `sh -c`: a pty session's payload is an argv,
        and a shell between the terminal and the program would add a process and
        reinterpret its arguments. And the cwd rides on `--wdns`, which resolves
        in the TARGET mount namespace, replacing the script's inner `cd`; `--wd=`
        and Popen's `cwd=` both resolve in the CALLER's and were measured handing
        the payload the host's view of a masked path. There is consequently no
        IMP_COMMAND/IMP_CWD in the env — no script reads them, and putting the
        argv in an environment variable when it is already on argv buys nothing.
        `--wdns` is util-linux 2.38 (2022) or newer; an older host fails this
        spawn loudly, with an nsenter usage dump.

        Everything else is `join`'s, verbatim: the same optional outer
        user-namespace stage, the same pinned ns fds named as /proc/self/fd/N,
        and the same `unshare --user --map-current-user` capability drop. That
        last one is not optional here — entering the keeper's own user namespace
        hands CAP_FULL_SET whenever bwrap built a single userns level (the
        production shape), which is enough to `umount` a secret_dirs mask and
        read what it hid, a capability the `bash` tool does not have. It is a
        plain exec with no fork, so the controlling terminal and process group a
        pty caller depends on are unaffected."""
        if self.closed:
            raise AssertionError("join_exec on a closed SandboxNamespace")
        spawn_argv: list[str] = []
        pass_fds = tuple(fd for _kind, fd in self.ns_fds)
        if self.outer_user_fd is not None:
            spawn_argv += ["nsenter", f"--user=/proc/self/fd/{self.outer_user_fd}",
                           "--preserve-credentials", "--"]
            pass_fds += (self.outer_user_fd,)
        spawn_argv += ["nsenter"]
        spawn_argv += [f"--{_NSENTER_FLAG[kind]}=/proc/self/fd/{fd}"
                       for kind, fd in self.ns_fds]
        spawn_argv += [f"--wdns={cwd}", "--preserve-credentials", "--",
                       "unshare", "--user", "--map-current-user", "--", *argv]
        return SandboxSpawn(argv=spawn_argv,
                            env=_spawn_child_env(self.policy, engine_env),
                            pass_fds=pass_fds)

    def open_pty(self) -> tuple[int, int]:
        """Allocate a pty from the KEEPER's devpts rather than imp's own, so the
        slave's node exists inside the namespace and the driven program can name
        its tty. Binding /dev/pts into the sandbox instead is forbidden: bwrap's
        --dev makes /dev/ptmx a SYMLINK into its own instance, so any bind there
        repoints ptmx at the host's ptmxmode=000 instance and breaks openpty() for
        everything in the sandbox. Allocation goes through the PINNED dirfd, never
        a re-resolved /proc/<pid>/root path — same pid-reuse window ns_fds avoids."""
        if self.closed:
            raise AssertionError("open_pty on a closed SandboxNamespace")
        master = os.open("ptmx", os.O_RDWR | os.O_NOCTTY, dir_fd=self.devpts_fd)
        try:
            fcntl.ioctl(master, TIOCSPTLCK, struct.pack("i", 0))
            n = struct.unpack("i", fcntl.ioctl(master, TIOCGPTN, struct.pack("i", 0)))[0]
            slave = os.open(str(n), os.O_RDWR | os.O_NOCTTY, dir_fd=self.devpts_fd)
        except OSError:
            os.close(master)   # the ptmx open already made the node; this reclaims it
            raise
        return master, slave

    @classmethod
    def open(cls, *, policy: SandboxPolicy, engine_env: Mapping[str, str],
             environ: Mapping[str, str],
             on_retry: Callable[[int, str], None] | None = None) -> SandboxNamespace:
        """Open a keeper namespace from `policy`, retrying the same spawn budget the
        per-call spawn used to carry. Retry is safe by construction: a failed open
        spawns no command. `on_retry(next_attempt, detail)` fires once per failed
        attempt so each caller keeps its own runner_warning wording; on exhaustion
        this raises SandboxOpenFailed carrying the detail, and the caller decides
        what to report. Blocking (the keeper handshake is ~7-15ms, the backoffs up
        to 400ms). The keeper must not OUTLIVE the thread that opened it:
        --die-with-parent is a PR_SET_PDEATHSIG, which the kernel fires when the
        creating THREAD exits, so a namespace opened in a thread-pool worker and
        handed onwards dies when that worker retires. One kept across calls is
        therefore opened on the thread that owns it (ensure_tool_namespace); one
        opened and closed inside a single call may open in a worker, since no
        window exists in which the worker could retire first (gates.run_command)."""
        if not policy.active:
            raise AssertionError("SandboxNamespace.open on an inactive policy")
        for attempt in range(1, SANDBOX_SPAWN_MAX_ATTEMPTS + 1):
            try:
                return cls._open_once(policy=policy, engine_env=engine_env,
                                      environ=environ)
            except SandboxOpenFailed as failure:
                if attempt == SANDBOX_SPAWN_MAX_ATTEMPTS:
                    raise
                if on_retry is not None:
                    on_retry(attempt + 1, failure.detail)
                time.sleep(sandbox_spawn_backoff_delay(attempt))
        raise AssertionError("open retry loop must return or raise on the last attempt")

    @classmethod
    def _open_once(cls, *, policy: SandboxPolicy, engine_env: Mapping[str, str],
                   environ: Mapping[str, str]) -> SandboxNamespace:
        # prepare_spawn_policy MUST run first: it mints the home-overlay layer whose
        # merged path is substituted into the bwrap argv, and the whole bwrap is then
        # wrapped in an `unshare … mount -t overlay … || exit 1; exec bwrap …` chain,
        # so the layer paths are INPUTS to building the argv. One layer per namespace.
        prepared = prepare_spawn_policy(policy)
        info_r, info_w = os.pipe()
        err_r, err_w = os.pipe()
        devnull = os.open(os.devnull, os.O_RDWR)
        spawn = build_sandbox_spawn(
            KEEPER_PAYLOAD, cwd=prepared.worktree, policy=prepared,
            engine_env=engine_env, environ=environ, keeper_info_fd=info_w)
        try:
            # The keeper must inherit NO caller pipe (a long-lived holder of a write
            # end wedges the reader forever), and start_new_session keeps it out of
            # any interrupted process group — --die-with-parent is its only leash.
            proc = subprocess.Popen(
                spawn.argv, env=spawn.env, pass_fds=spawn.pass_fds,
                stdin=devnull, stdout=devnull, stderr=err_w,
                start_new_session=True)
        except OSError as exc:
            os.close(info_r)
            os.close(err_r)
            raise SandboxOpenFailed(str(exc)) from exc
        finally:
            os.close(info_w)
            os.close(err_w)     # our copy; the keeper keeps its own for life
            os.close(devnull)
        deadline = time.monotonic() + _KEEPER_READY_TIMEOUT_S
        ns_fds: list[tuple[str, int]] = []
        outer_user_fd: int | None = None
        devpts_fd: int | None = None
        try:
            info = _read_to_eof(info_r, deadline)
            stderr = _await_sentinel(err_r, deadline)
            if sandbox_spawn_failed(stderr, sandbox_active=True):
                raise SandboxOpenFailed(sandbox_failure_detail(stderr))
            # bwrap's --info-fd child, NOT the outer unshare: that is what makes the
            # pin land on the real ns-init and leaves imp as bwrap's direct parent.
            # Reaching here means the sentinel arrived, so bwrap finished writing the
            # JSON; a ValueError or LookupError is still caught below rather than
            # escaping open()'s retry and leaking a live keeper.
            child_pid = int(json.loads(info)["child-pid"])
            kinds = list(_KEEPER_NS_KINDS)
            if not prepared.network:
                kinds.append("net")
            for kind in kinds:
                ns_fds.append((kind, os.open(f"/proc/{child_pid}/ns/{kind}", os.O_RDONLY)))
            devpts_fd = os.open(f"/proc/{child_pid}/root/dev/pts",
                                os.O_PATH | os.O_DIRECTORY)
            outer_user_fd = _ns_owner_fd(dict(ns_fds))
        except SandboxOpenFailed:
            _abandon_keeper(proc, ns_fds, err_r, devpts_fd)
            raise
        except (OSError, ValueError, LookupError) as exc:
            _abandon_keeper(proc, ns_fds, err_r, devpts_fd)
            raise SandboxOpenFailed(str(exc)) from exc
        finally:
            os.close(info_r)     # bwrap has closed its end; nothing writes here again
        # err_r stays OPEN and is owned by the namespace: the keeper holds the write
        # end for its whole life, so closing the read end here would make any later
        # write by the bwrap monitor raise EPIPE and take the whole namespace down.
        return cls(policy=prepared, base_policy=policy, ns_fds=tuple(ns_fds),
                   outer_user_fd=outer_user_fd, devpts_fd=devpts_fd,
                   ns_init_pid=child_pid, err_fd=err_r, proc=proc)

    def _is_ns_init(self) -> bool:
        """ns_init_pid still names THIS namespace's init, i.e. the pid has not been
        recycled since open(). Compared against the pid-namespace fd pinned at
        open(), so a stranger that inherited the pid can never be signalled: it
        would have to live in our own pid namespace, which cannot outlive its init."""
        try:
            live = os.stat(f"/proc/{self.ns_init_pid}/ns/pid")
        except OSError:
            return False
        pinned = os.fstat(dict(self.ns_fds)["pid"])
        return (live.st_dev, live.st_ino) == (pinned.st_dev, pinned.st_ino)

    def close(self) -> None:
        """SIGKILL the ns-init: killing pid 1 of a pid namespace makes the kernel kill
        everything inside, so setsid / nohup / double-fork / re-grouping stop
        mattering. SIGTERM would be a no-op (a pid-ns init is SIGNAL_UNKILLABLE for
        default-action signals) and killing the outer bwrap would leave the init
        alive and still joinable. Idempotent."""
        if self.closed:
            return
        self.closed = True
        if self._is_ns_init():
            with contextlib.suppress(OSError):
                os.kill(self.ns_init_pid, signal.SIGKILL)
        for _kind, fd in self.ns_fds:
            os.close(fd)
        if self.outer_user_fd is not None:
            os.close(self.outer_user_fd)
        os.close(self.devpts_fd)
        os.close(self.err_fd)
        try:
            self.proc.wait(timeout=_KEEPER_REAP_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait()


def prepare_spawn_policy(policy: SandboxPolicy) -> SandboxPolicy:
    """Ready a frozen policy for THIS spawn: re-create imp's own re-exposed ref
    directories and the per-quest scratch dir, and drop any mask/ro-bind path that
    has since vanished.

    git_ref_reexpose (.git/refs/heads/imp) is created once, at policy-bake time in
    resolve_policy — but git prunes a ref directory the moment its last loose ref
    goes, so a mid-run `git pack-refs` (auto-gc) packs every refs/heads/imp/* ref
    into packed-refs and takes the now-empty directory with it. bwrap must bind
    every path in the plan, so from then on EVERY spawn dies in sandbox setup for
    the life of the policy — permanent, not transient, so the retry loop cannot
    clear it. It is re-created here rather than pruned like the mask categories
    below: pruning would leave refs/heads read-only, so a git commit on the quest
    branch inside the sandbox could not write its loose ref. The directory is
    imp's own and always safe to create.

    After the fail-closed state allowlist, secret_dirs/secret_files name only
    self-owned never-vanishing paths — the current quest's own steps/ +
    events.jsonl, the home secret dirs/files, and /run/user/$UID — so the prune
    over those two sets is effectively a no-op. Its remaining job is
    git_ro_remask: its existence-gated refs/index masks (refs/heads, refs/stash,
    index) can vanish mid-run — e.g. a branch or the stash ref is deleted after
    policy creation; bwrap must still bind each ro path over the read-only floor,
    so a spawn carrying a vanished path dies in sandbox SETUP ("Read-only file
    system") before the command runs. A mask over a gone path protects nothing, so
    existence-gating at spawn is always safe. Own binds (worktree, quest_home) and stable config
    (caches, extra_*) never churn and are left untouched. Scoped to the spawn argv only; the
    in-process allows_read/allows_write mirrors keep the unpruned policy (a gone path is denied
    either way).

    The repo marker re-expose (state_repo_ro) is gated on the same terms _checked_override
    reads it on — is_file() — so the bind exists exactly when the guard has something to
    read; a hand-pinned $IMP_STATE_DIR carries no marker, and an un-gated --ro-bind of a
    missing source would abort bwrap setup for those users.

    It also MINTS the home-overlay layer when policy.overlay_root is set: a fresh
    mkdtemp dir under the root holding its own upper/work/merged. `open()` is the
    only caller, so the layer is minted once per namespace, not per spawn — one
    live overlay mount per keeper rather than overlapping ones sharing an
    upperdir/workdir (the kernel's ovl_inuse warning), and every command in the run
    joins that one mount. No per-layer cleanup; step teardown rmtrees the root."""
    if not policy.active:
        return policy
    for ref_dir in policy.git_ref_reexpose:
        Path(ref_dir).mkdir(parents=True, exist_ok=True)
    if policy.quest_tmp_dir:
        # The executor empties this dir between steps and reaps it at quest end, so
        # --bind (not --bind-try) would abort setup on a missing source. The chmod
        # covers the imp-<uid> PARENT too: mkdir(mode=) applies only to the leaf, so
        # a recreated parent would land at the umask default.
        ensure_quest_tmp_dir(Path(policy.quest_tmp_dir))
    home_layer = policy.home_layer
    if policy.overlay_root is not None:
        root = Path(policy.overlay_root.path)
        root.mkdir(parents=True, exist_ok=True)   # self-heal, like git_ref_reexpose
        layer_dir = Path(tempfile.mkdtemp(dir=root))
        upper = layer_dir / "upper"
        work = layer_dir / "work"
        merged = layer_dir / "merged"
        for d in (upper, work, merged):
            d.mkdir()
        home_layer = HomeLayer(home=policy.home, merged=str(merged),
                               upper=str(upper), work=str(work),
                               userxattr=policy.overlay_root.userxattr)
    return replace(
        policy,
        home_layer=home_layer,
        git_ro_remask=_existing(list(policy.git_ro_remask)),
        secret_dirs=_existing(list(policy.secret_dirs)),
        secret_files=_existing(list(policy.secret_files)),
        state_repo_ro=(policy.state_repo_ro
                       if Path(policy.state_repo_ro).is_file() else ""),
    )


# Code-default secret denylist (spec B.3). Paths are home-relative; resolve_policy
# joins them onto the home dir and keeps only those that exist. /run/user/$UID is
# added separately (it masks live agent sockets, H2). secret_files are masked with
# --ro-bind /dev/null; everything else is a directory --tmpfs.
_DEFAULT_SECRET_DIRS = (
    ".ssh", ".aws", ".gnupg", ".kube", ".config/gcloud", ".config/gh",
    ".config/containers", ".local/share/keyrings",
)
_DEFAULT_SECRET_FILES = (
    ".docker/config.json", ".config/containers/auth.json", ".netrc",
    ".git-credentials", ".npmrc", ".pypirc",
)
# Code-default writable toolchain caches (spec B.3a).
_DEFAULT_CACHES = (
    ".cache", ".cargo", ".rustup", ".npm", ".local/share", ".local/state",
    ".m2", ".gradle", "go",
)
# Generic env names passed through verbatim from the ambient environment.
_GENERIC_ENV = ("PATH", "HOME", "USER", "LOGNAME", "LANG", "TERM", "TZ")


class SandboxUnavailable(Exception):
    """bwrap is required (sandbox.enabled, allow_unsandboxed=False) but absent."""


@dataclass(frozen=True, slots=True)
class SandboxConfig:
    enabled: bool = True
    allow_unsandboxed: bool = False
    network: bool = True
    secret_paths: tuple[str, ...] = ()        # EXTENDS the code default dirs+files
    writable_caches: tuple[str, ...] = ()     # REPLACES the code default caches when non-empty
    extra_readable: tuple[str, ...] = ()
    extra_writable: tuple[str, ...] = ()
    env_passthrough: tuple[str, ...] = ()
    # Injected for testability; default to the live host values.
    home: Path | None = None
    uid: int | None = None


@lru_cache(maxsize=1)
def sandbox_available() -> bool:
    """Linux + bwrap on PATH + a one-shot userns probe succeeds. Cached.

    Tests that monkeypatch shutil.which or subprocess.run must call
    sandbox_available.cache_clear() first (the result is process-global cached).
    """
    if platform.system() != "Linux" or shutil.which("bwrap") is None:
        return False
    try:
        r = subprocess.run(
            ["bwrap", "--unshare-user", "--ro-bind", "/", "/", "true"],
            capture_output=True, timeout=10,
        )
        return r.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def _force_rmtree(path: Path) -> None:
    """rmtree that also clears the mode-000 `work/work` dir overlayfs leaves in
    its workdir (untraversable until chmod'd), so no overlay residue survives
    under the state dir. Owner is the real uid, so the chmod always succeeds."""
    for root, dirs, _files in os.walk(path):
        for d in dirs:
            p = os.path.join(root, d)
            if os.path.islink(p):  # os.chmod follows symlinks; skip to avoid mutating outside tree
                continue
            with contextlib.suppress(OSError):
                os.chmod(p, 0o700)
    shutil.rmtree(path, ignore_errors=True)


@lru_cache(maxsize=1)
def overlay_mode(probe_base: Path, home: Path) -> bool | None:
    """Whether an unprivileged overlay mounts on this host, and in WHICH shape:
    None → none does (the caller falls back to a rw bind of the real home);
    True → mount it with `-o userxattr`; False → mount it without.

    Needs Linux + `unshare` on PATH + a one-shot unprivileged `mount -t overlay`
    inside a user namespace using the agent's REAL home as lowerdir. It probes
    WITH userxattr first — that keeps the opaque marker in user.overlay.*, which
    an unprivileged mount can actually write, so removing and recreating a
    lower-present directory succeeds instead of returning EIO — and re-probes
    WITHOUT it only when that mount exits nonzero (the option landed in kernel
    v5.11; older kernels reject the mount outright, and losing the overlay would
    cost home isolation). The scratch dirs are scrubbed and recreated between the
    two attempts so the second starts from a clean upper/work. An OSError or
    SubprocessError (incl. TimeoutExpired) means the host cannot run the probe at
    all — the second attempt would fail identically — so it short-circuits to None
    without retrying.

    The upper/work/merged scratch dirs live under `probe_base/.overlay-probe` (a
    real dir under the quest home, NOT /tmp — some hosts refuse an overlay upperdir on
    tmpfs), but the lowerdir is `home` — the SAME shape the keeper's mount in
    build_sandbox_spawn uses — so the probe exercises the overlapping-layers
    condition the real mount hits instead of a synthetic empty lower that overlaps
    nothing. Cached like sandbox_available(); the answer is carried to the keeper's
    mount (via make_home_layer), so probe and spawn always agree.

    Tests that monkeypatch subprocess.run / shutil.which / platform.system must
    call overlay_mode.cache_clear() first (the result is process-global cached,
    keyed on (probe_base, home)).
    """
    if platform.system() != "Linux" or shutil.which("unshare") is None:
        return None
    probe = probe_base / ".overlay-probe"
    upper, work, merged = probe / "upper", probe / "work", probe / "merged"
    try:
        for opt in (",userxattr", ""):
            _force_rmtree(probe)
            for d in (upper, work, merged):
                d.mkdir(parents=True, exist_ok=True)
            r = subprocess.run(
                ["unshare", "--user", "--map-root-user", "--mount", "sh", "-c",
                 f"mount -t overlay overlay -o "
                 f"lowerdir={shlex.quote(str(home))},"
                 f"upperdir={shlex.quote(str(upper))},"
                 f"workdir={shlex.quote(str(work))}{opt} "
                 f"{shlex.quote(str(merged))}"],
                capture_output=True, timeout=10,
            )
            if r.returncode == 0:
                return opt == ",userxattr"
        return None
    except (OSError, subprocess.SubprocessError):
        return None
    finally:
        _force_rmtree(probe)


def make_home_layer(
    home: str, *, root: Path, userxattr: bool | None,
) -> tuple[HomeLayer, OverlayRoot | None, Callable[[], None]]:
    """Build one invocation's writable-home layer, the root its keepers mint into,
    and the teardown callable. `userxattr` is overlay_mode's tri-state. None → the
    plain rw bind of the real home (fallback): no dirs, no overlay root, a no-op
    cleanup. True/False → create `root` and report it as the policy's overlay_root,
    carrying the negotiated flag so every mount uses the exact shape the probe
    validated; prepare_spawn_policy then mints an upper/work/merged under the root
    for each policy it prepares — once per keeper namespace on the tool path — and
    the returned cleanup rmtrees the whole root once (no per-layer cleanup exists,
    so a hard-killed command cannot leak a layer).
    The layer is the same plain HomeLayer in BOTH branches. The CALLER probes
    (overlay_mode at its own probe location) and chooses where the root lives, so
    this primitive is shared by the executor (root sibling to transcript_dir) and
    the isolated-subagent path (root sibling to the spike worktree)."""
    if userxattr is None:
        return HomeLayer(home=home), None, (lambda: None)
    root.mkdir(parents=True, exist_ok=True)
    return (HomeLayer(home=home), OverlayRoot(path=str(root), userxattr=userxattr),
            lambda: _force_rmtree(root))


def _existing(paths: list[str]) -> tuple[str, ...]:
    return tuple(p for p in paths if Path(p).exists())


def sandbox_config_from_raw(raw: Mapping[str, Any]) -> SandboxConfig:
    """Build a SandboxConfig from a raw ``sandbox:`` mapping, applying resolve_policy defaults."""
    return SandboxConfig(
        enabled=raw.get("enabled", True),
        allow_unsandboxed=raw.get("allow_unsandboxed", False),
        network=raw.get("network", True),
        secret_paths=tuple(raw.get("secret_paths", ())),
        writable_caches=tuple(raw.get("writable_caches", ())),
        extra_readable=tuple(raw.get("extra_readable", ())),
        extra_writable=tuple(raw.get("extra_writable", ())),
        env_passthrough=tuple(raw.get("env_passthrough", ())),
    )


def _state_mask(control_root: Path) -> Path:
    """The fail-closed tmpfs root: THIS project's state directory ($IMP_STATE_DIR
    when pinned, else imp_home()/projects/<slug>). Created if absent — bwrap cannot
    mount a tmpfs over a missing directory. Returned RESOLVED: bind_plan compares it
    against the already-resolved worktree, and a symlinked $HOME would otherwise
    make a real quest's worktree read as non-nested and mask it away."""
    # Both this and _projects_ro read os.environ (via project_state_dir's
    # IMP_STATE_DIR and imp_home's IMP_HOME), NOT resolve_policy's `environ` arg:
    # the seam owns those variables, so the two cannot disagree.
    state = project_state_dir(control_root)
    state.mkdir(parents=True, exist_ok=True)
    return state.resolve()


def _projects_ro() -> str:
    """imp's records of OTHER projects, read-only. Always imp_home()'s, never an
    $IMP_STATE_DIR override's arbitrary parent; "" when absent, since a missing
    bind source is a bwrap setup failure. Resolved, like _state_mask."""
    projects = imp_home() / "projects"
    return str(projects.resolve()) if projects.is_dir() else ""


def resolve_policy(
    *, worktree: Path, control_root: Path, quest_id: str, cfg: SandboxConfig,
    engine_env_result: Mapping[str, str], environ: Mapping[str, str],
    available: bool, quest_dir: Path | None = None, disposable_tree: bool = False,
) -> SandboxPolicy:
    """Stat once, bake a frozen SandboxPolicy. The ONE impure step (spec B.1).

    Three-way posture (spec B.7): enabled+available → active; enabled+unavailable+
    allow_unsandboxed → bare (note='unsandboxed_by_config'); enabled+unavailable+
    !allow → raise SandboxUnavailable; !enabled → bare (note='sandbox_disabled').
    """
    if not cfg.enabled:
        return _inactive(note="sandbox_disabled")
    if not available:
        if cfg.allow_unsandboxed:
            return _inactive(note="unsandboxed_by_config")
        raise SandboxUnavailable(
            "bwrap unavailable; install bubblewrap (e.g. `dnf install bubblewrap` "
            "/ `apt install bubblewrap`) or set sandbox.allow_unsandboxed in your "
            "user/local config"
        )

    if quest_dir is not None:
        quest_dir = quest_dir.resolve()
    home = cfg.home or Path.home()
    uid = cfg.uid if cfg.uid is not None else os.getuid()

    git = control_root / ".git"
    remask = [str(git / "hooks"), str(git / "config")]

    # Git-ref foot-gun guard: RO-mask the crown-jewel shared paths (all of
    # refs/heads, refs/stash, the main index) via the existing git_ro_remask
    # ro-bind category, and RW re-expose refs/heads/imp so the agent's own
    # quest/spike branches still update. bind_plan() emits the re-expose after
    # the refs/heads mask, so last-wins keeps the imp/ subtree writable while
    # master and every other branch stay read-only. Existence-gated like caches/
    # secrets. Applied UNIFORMLY — including the draft/degenerate case where the
    # agent's project_root IS the control root (the threshold concierge and its
    # non-isolated explorer/researcher subagents). This is a deliberate asymmetry
    # with _enforce_clean_tree, which keeps its project_root == control_root skip:
    # the ref-mask must cover draft agents precisely because the draft concierge must
    # make no commits to the project tree — the mask stops a raw bash `git commit` from
    # bypassing that no-commit contract, whereas clean-tree tolerance for the user's
    # pre-existing WIP is unchanged.
    refs_heads = git / "refs" / "heads"
    remask += list(_existing(
        [str(refs_heads), str(git / "refs" / "stash"), str(git / "index")]))
    imp_refs = refs_heads / "imp"
    imp_refs.mkdir(parents=True, exist_ok=True)
    git_ref_reexpose: tuple[str, ...] = (str(imp_refs),)

    cache_names = cfg.writable_caches or _DEFAULT_CACHES
    caches = _existing([str(home / c) for c in cache_names])

    # Secret dirs/files: code defaults + config secret_paths (each classified
    # dir-vs-file by an actual stat); + /run/user/$UID. Applied uniformly for
    # every agent — the disposable-tree capability no longer suppresses them.
    secret_dirs = list(_existing([str(home / d) for d in _DEFAULT_SECRET_DIRS]))
    secret_files = list(_existing([str(home / f) for f in _DEFAULT_SECRET_FILES]))
    for extra in cfg.secret_paths:
        is_abs = extra.startswith("/")
        p = Path(extra) if is_abs else (home / extra)
        if p.is_dir():
            secret_dirs.append(str(p))
        elif p.exists():
            secret_files.append(str(p))
    secret_dirs.append(f"/run/user/{uid}")   # H2 — agent sockets; mask even if abs

    # Re-mask the current quest's OWN footguns by fixed name (self-owned, always-
    # present, never-churning): steps/ is an overlay-home mountpoint a raw walk
    # would stall on; events.jsonl is the unbounded footgun log whose sole in-
    # sandbox reader is the CLI (routed through the daemon store). Emitted last (via
    # secret_dirs/secret_files) so they re-mask on top of the quest_home rebind.
    # The state tmpfs (state_mask) hides every sibling quest of THIS project and
    # its archive for free — no glob, no enumeration — so a sibling appearing or
    # reaping mid-run changes nothing: the mask TOCTOU race is gone by construction.
    if quest_dir is not None:
        secret_dirs += list(_existing([str(quest_dir / "steps")]))
        secret_files += list(_existing([str(quest_dir / "events.jsonl")]))

    # ro overlay of workflow snapshot — bind_plan emits it after the quest_home rw
    # bind, so last-covering-wins makes only workflow/ read-only.
    readonly_overlays: tuple[str, ...] = ()
    if quest_dir is not None and (quest_dir / "workflow").is_dir():
        readonly_overlays = (str(quest_dir / "workflow"),)

    state_mask = str(_state_mask(control_root))    # creates imp_home()/projects
    projects_ro = _projects_ro()                   # ...which this then existence-gates

    session_sock = session_socket_path(quest_id)
    # TMPDIR: per-quest scratch dir, derived EXACTLY like Quest.tmp_dir
    # (Path(tempfile.gettempdir()) / imp-<uid> / <id>) so it tracks the conftest
    # temp-redirect fixture and points at the dir the executor creates/wipes. The
    # SAME value is the bind source below — one derivation, two uses.
    quest_tmp = Path(tempfile.gettempdir()) / f"imp-{os.getuid()}" / quest_id
    var_tmp_root = quest_var_tmp_root()
    quest_var_tmp_dir = (
        str(var_tmp_root / f"imp-{os.getuid()}" / quest_id)
        if quest_dir is not None and os.access(var_tmp_root, os.W_OK)
        else "")

    return SandboxPolicy(
        active=True,
        worktree=str(worktree),
        git_common_dir=str(git),
        quest_home=str(quest_dir) if quest_dir is not None else "",
        state_mask=state_mask,
        state_repo_ro=f"{state_mask}/repo",
        projects_ro=projects_ro,
        git_ro_remask=tuple(remask),
        git_ref_reexpose=git_ref_reexpose,
        git_worktrees_mask=str(git / "worktrees"),
        git_worktree_self=(
            str(git / "worktrees" / Path(worktree).name)
            if quest_dir is not None and worktree != control_root else ""),
        disposable_tree=disposable_tree,
        home=str(home),
        readonly_overlays=readonly_overlays,
        writable_caches=caches,
        extra_readable=tuple(cfg.extra_readable),
        extra_writable=tuple(cfg.extra_writable),
        secret_dirs=tuple(secret_dirs),
        secret_files=tuple(secret_files),
        quest_tmp_dir=str(quest_tmp),
        quest_var_tmp_dir=quest_var_tmp_dir,
        child_env=_build_child_env(
            engine_env_result, environ, cfg.env_passthrough,
            session_socket=str(session_sock), tmpdir=str(quest_tmp),
            shim_dir=str(imp_shim_dir(quest_dir)) if quest_dir is not None else ""),
        network=cfg.network,
        note="",
    )


def _inactive(*, note: str) -> SandboxPolicy:
    return SandboxPolicy(
        active=False, worktree="", git_common_dir="", git_ro_remask=(),
        writable_caches=(), extra_readable=(), extra_writable=(),
        secret_dirs=(), secret_files=(), child_env=(), network=True, note=note,
    )


def _build_child_env(
    engine_env_result: Mapping[str, str], environ: Mapping[str, str],
    env_passthrough: tuple[str, ...], session_socket: str = "",
    shim_dir: str = "", tmpdir: str = "",
) -> tuple[tuple[str, str], ...]:
    """The child environment (spec B.4). IMP_* and PYTHONPATH come from engine_env_result
    (authoritative; PYTHONPATH is the single deliberate non-IMP_ exception — the
    imp-on-imp import shadow, a bare worktree prefix merged ahead of any ambient
    PYTHONPATH by the same helper overlay_engine_env uses); generic + LC_* +
    env_passthrough pass through from `environ`. With the socket and every IMP_* path
    now identical inside and outside the sandbox, nothing is rewritten — real host
    paths pass through verbatim.

    Dedup: accumulated into a dict (insertion-ordered); re-assigning a key updates
    its value in place (first-seen position, last value wins). TMPDIR (the per-quest
    scratch dir) is assigned last so it wins over any ambient or env_passthrough value.
    """
    d: dict[str, str] = {}
    for k in _GENERIC_ENV:
        if k in environ:
            d[k] = environ[k]
    for k, v in environ.items():
        if k.startswith("LC_"):
            d[k] = v
    for k in env_passthrough:
        if k in environ:
            d[k] = environ[k]
    for k, v in engine_env_result.items():
        if _is_engine_passthrough(k):
            d[k] = v
    pythonpath_prefix = engine_env_result.get("PYTHONPATH")
    if pythonpath_prefix:
        d["PYTHONPATH"] = merge_pythonpath_prefix(
            pythonpath_prefix, environ.get("PYTHONPATH", ""))
    if session_socket:
        d["IMP_SESSION_SOCKET"] = session_socket
    if shim_dir:
        d["PATH"] = (shim_dir + os.pathsep + d["PATH"]) if d.get("PATH") else shim_dir
    if tmpdir:
        d["TMPDIR"] = tmpdir   # per-quest scratch; last → wins over ambient/env_passthrough
    return tuple(d.items())
