# SPDX-License-Identifier: MIT
# Copyright 2026 Josh Poimboeuf
"""Shell sandbox (bubblewrap) — pure policy core + argv builder (spec Part B).

Two spawn points consume this: the `bash` tool (agents/tools/bash.py) and script
gates (engine/gates.py). `resolve_policy` does the one-time stat I/O and bakes a
frozen `SandboxPolicy`; `build_sandbox_argv` is pure given that policy.

The flag ORDER in build_sandbox_argv is load-bearing: bwrap is last-wins, so the
writable-home bind (when a home_layer is present) and then the /tmp bind are
emitted EARLY (right after the ro-floor, before worktree/git/remask) and the
secret masks MUST be emitted AFTER every bind (incl. extra_writable), else
a config-driven bind could re-expose a masked secret dir (spec L1-r5). /tmp is
an identity bind to the real host /tmp. No --unshare-pid / --die-with-parent /
--new-session — all three break the runner's interrupt grace (spec B.9); reaping
is via the process group.
"""

from __future__ import annotations

import contextlib
import os
import platform
import random
import shlex
import shutil
import subprocess
import tempfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from functools import lru_cache
from pathlib import Path
from typing import Any

from imp.engine.env import HEADLESS_ENV, quest_var_tmp_root
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
    setenv: tuple[tuple[str, str], ...]       # resolved env allowlist key/value pairs
    network: bool                             # True → no --unshare-net
    note: str = "" # "" | "unsandboxed_by_config" | "sandbox_disabled" | "unsandboxed_override"
    quest_home: str = ""                     # <state dir>/quests/<id> rw bind ("" → none)
    state_mask: str = ""                     # fail-closed tmpfs mask ("" → none)
    projects_ro: str = ""                    # ro bind of imp_home()/projects ("" → none)
    readonly_overlays: tuple[str, ...] = ()  # ro subtrees; after git_ro_remask, before caches
    git_ref_reexpose: tuple[str, ...] = ()   # rw re-expose of refs/heads/imp; after git_ro_remask
    disposable_tree: bool = False            # agent runs in a disposable git worktree
    home: str = ""                           # real $HOME ("" → inactive/unresolved)
    home_layer: HomeLayer | None = None      # writable-home layer (None → ro read-through)
    quest_var_tmp_dir: str = ""  # per-quest dir onto /var/tmp ("" → no bind; host ro)
    git_worktrees_mask: str = ""  # <control>/.git/worktrees tmpfs mask ("" → none)
    git_worktree_self: str = ""   # rw re-expose of .git/worktrees/<self> ("" → none)

    def bind_plan(self) -> list[tuple[str, str]]:
        """The ordered path-covering binds, in bwrap EMIT ORDER — the SINGLE
        source of truth shared by build_sandbox_argv (which formats each entry to
        argv), allows_write, and allows_read (both evaluate last-covering-wins).
        kind is one of rw / ro / mask_dir / mask_file; rw grants write. The order is
        load-bearing: the ro overlays (git_ro_remask, readonly_overlays, extra_readable)
        are NOT globally last-wins — a later writable bind re-exposes a path they
        cover — only the secret masks are unconditionally last. Add a new bind here
        and ALL consumers pick it up; the argv-parity test enforces no drift. The
        home_layer rw bind goes FIRST (so caches/worktree/secrets over $HOME win),
        /tmp next, then the per-quest read-write /var/tmp scratch bind (emitted only
        when quest_var_tmp_dir is set; empty → no /var/tmp bind, host /var/tmp stays
        on the read-only floor), and the secret masks unconditionally LAST.

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
        quest's own home (minus its own steps/events) plus the current worktree iff
        the worktree is a descendant of the mask; everything else under it is masked.
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
        ]
        nested = bool(self.state_mask) and Path(self.worktree).is_relative_to(
            Path(self.state_mask))
        worktree_block = (
            [*state_block, (self.worktree, "rw")] if nested
            # draft: worktree is OUTSIDE the mask, so bind it BEFORE state_block
            else [(self.worktree, "rw"), *state_block])
        return [
            *([(self.home_layer.home, "rw")] if self.home_layer is not None else []),
            ("/tmp", "rw"),
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
    sandboxed git/tool invocation non-interactive. Shared by both allowlist
    sites (the per-spawn loop in build_sandbox_argv and the policy bake in
    _build_setenv) so they cannot drift."""
    return key.startswith("IMP_") or key == "PYTHONPATH" or key in HEADLESS_ENV


SANDBOX_ENTERED_MARKER = "__IMP_SANDBOX_ENTERED__"
SANDBOX_SPAWN_FAILED_MESSAGE = (
    "the OS sandbox could not start your command; it did NOT run "
    "(a sandbox-setup failure, not your command's own exit)"
)


def sandbox_spawn_failed(stderr: str, *, sandbox_active: bool) -> bool:
    """True iff sandbox was active but the entry sentinel is absent (command never ran)."""
    return sandbox_active and SANDBOX_ENTERED_MARKER not in stderr


def strip_sandbox_marker(stderr: str) -> str:
    """Remove the single entry-sentinel line from captured stderr (display-only, idempotent)."""
    return stderr.replace(SANDBOX_ENTERED_MARKER + "\n", "", 1)


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


def build_sandbox_argv(
    command: str, *, cwd: str, policy: SandboxPolicy,
    engine_env: Mapping[str, str],
) -> list[str]:
    """Return the argv to spawn `command`. When the policy is active, wrap with
    bwrap per spec §B.2 (fixed flag order); otherwise the bare ["sh","-c",cmd].

    `engine_env` is the LIVE, per-spawn engine_env (an `engine_env()` result) and
    is the single source of truth for the sandbox's `IMP_*` vars: every IMP_*-
    prefixed key it carries is emitted via --setenv, authoritatively superseding
    whatever was baked into `policy.setenv` at run start. The policy is resolved
    once per run (the stat-once design), but name-on-intent makes IMP_QUEST_TITLE
    mutate mid-run on a rename — so a gate or tool spawned after
    the rename MUST see the live values, not the empty ones frozen at run start.

    Required (not optional) on purpose: a sandboxed spawn without its live engine
    env is always a bug, so every call site — present and future — is forced to
    supply it rather than silently inheriting a stale snapshot. The allowlist is
    the IMP_ prefix plus PYTHONPATH (`_is_engine_passthrough`): any new IMP_* var
    flows through with no edit here, and PYTHONPATH passes through as the single
    deliberate non-IMP_ exception — the imp-on-imp import shadow — matching
    `_build_setenv`."""
    if not policy.active:
        return ["sh", "-c", command]

    argv: list[str] = ["bwrap"]
    # 1. whole host readable
    argv += ["--ro-bind", "/", "/"]
    # 2-10. path-covering binds, in emit order from policy.bind_plan() (the single
    #    source shared with allows_write). Emit order is load-bearing and last-wins:
    #    the home_layer rw bind (when present) goes FIRST so caches/worktree/secrets
    #    over $HOME win; /tmp goes next (identity bind to the real host /tmp) before
    #    the worktree/git binds and secret masks; secret masks go LAST.
    for bind, kind in policy.bind_plan():
        if kind == "rw":
            hl = policy.home_layer
            if hl is not None and hl.is_overlay and bind == hl.home:
                src = hl.merged                       # overlay merged view onto $HOME
            elif bind == "/var/tmp" and policy.quest_var_tmp_dir:
                src = policy.quest_var_tmp_dir         # per-quest host dir onto namespace /var/tmp
            else:
                src = bind                            # identity — /tmp is the real host /tmp
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
    # 13. env allowlist. The IMP_* slice comes authoritatively from the live
    # per-spawn engine_env (prefix allowlist) so a post-rename title wins over
    # the value frozen into policy.setenv at run start (name-on-intent). Dict
    # preserves first-seen order; reassigning a key updates in place, so no
    # duplicate --setenv is emitted.
    setenv = dict(policy.setenv)
    for k, v in engine_env.items():
        if _is_engine_passthrough(k):
            setenv[k] = v
    argv += ["--clearenv"]
    for k, v in setenv.items():
        argv += ["--setenv", k, v]
    # 14. namespaces — NO --unshare-pid / --die-with-parent / --new-session (B.9)
    argv += ["--unshare-user", "--unshare-ipc", "--unshare-uts"]
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
        return ["unshare", "--user", "--map-root-user", "--mount",
                "sh", "-c", mount]
    return argv


def prepare_spawn_policy(policy: SandboxPolicy) -> SandboxPolicy:
    """Ready a frozen policy for THIS spawn: re-create imp's own re-exposed ref
    directories, and drop any mask/ro-bind path that has since vanished.

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
    existence-gating at spawn is always safe. Own binds (worktree, quest_home) and
    stable config (caches, extra_*) never churn and are left untouched. Scoped to
    the spawn argv only; the in-process allows_read/allows_write mirrors keep the
    unpruned policy (a gone path is denied either way)."""
    if not policy.active:
        return policy
    for ref_dir in policy.git_ref_reexpose:
        Path(ref_dir).mkdir(parents=True, exist_ok=True)
    return replace(
        policy,
        git_ro_remask=_existing(list(policy.git_ro_remask)),
        secret_dirs=_existing(list(policy.secret_dirs)),
        secret_files=_existing(list(policy.secret_files)),
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
            with contextlib.suppress(OSError):
                os.chmod(os.path.join(root, d), 0o700)
    shutil.rmtree(path, ignore_errors=True)


@lru_cache(maxsize=1)
def overlay_available(probe_base: Path, home: Path) -> bool:
    """Linux + `unshare` on PATH + a one-shot unprivileged `mount -t overlay`
    inside a user namespace succeeds using the agent's REAL home as lowerdir.
    The upper/work/merged scratch dirs live under `probe_base/.overlay-probe` (a
    real dir under the quest home, NOT /tmp — some hosts refuse an overlay upperdir on
    tmpfs), but the lowerdir is `home` — the SAME shape the per-command mount in
    build_sandbox_argv uses — so the probe exercises the overlapping-layers
    condition the real mount hits instead of a synthetic empty lower that overlaps
    nothing. Cached like sandbox_available(); Part 2 calls this to choose the COW
    home vs. the rw fallback.

    Tests that monkeypatch subprocess.run / shutil.which / platform.system must
    call overlay_available.cache_clear() first (the result is process-global
    cached, keyed on (probe_base, home)).
    """
    if platform.system() != "Linux" or shutil.which("unshare") is None:
        return False
    probe = probe_base / ".overlay-probe"
    upper, work, merged = probe / "upper", probe / "work", probe / "merged"
    try:
        for d in (upper, work, merged):
            d.mkdir(parents=True, exist_ok=True)
        r = subprocess.run(
            ["unshare", "--user", "--map-root-user", "--mount", "sh", "-c",
             f"mount -t overlay overlay -o "
             f"lowerdir={shlex.quote(str(home))},"
             f"upperdir={shlex.quote(str(upper))},"
             f"workdir={shlex.quote(str(work))} {shlex.quote(str(merged))}"],
            capture_output=True, timeout=10,
        )
        return r.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False
    finally:
        _force_rmtree(probe)


def make_home_layer(
    home: str, *, upper: Path, work: Path, merged: Path, use_overlay: bool,
) -> tuple[HomeLayer, Callable[[], None]]:
    """Build one invocation's writable-home layer + its teardown callable.
    use_overlay=True → a COW overlay: create upper/work/merged and return a
    HomeLayer that build_sandbox_argv wraps with unshare + mount -t overlay
    (lowerdir=home); the returned cleanup rmtrees the three dirs. use_overlay=False
    → a plain rw bind of the real home (fallback): no dirs are created and cleanup
    is a no-op. The CALLER decides use_overlay (via overlay_available at its own
    probe location) and chooses where the dirs live, so this primitive is shared by
    the executor (dirs sibling to transcript_dir) and the isolated-subagent path
    (dirs sibling to the spike worktree)."""
    if use_overlay:
        for d in (upper, work, merged):
            d.mkdir(parents=True, exist_ok=True)
        layer = HomeLayer(home=home, merged=str(merged),
                          upper=str(upper), work=str(work))

        def cleanup() -> None:
            for d in (upper, work, merged):
                _force_rmtree(d)

        return layer, cleanup
    return HomeLayer(home=home), (lambda: None)


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
    # temp-redirect fixture and points at the dir the executor creates/wipes.
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
        quest_var_tmp_dir=quest_var_tmp_dir,
        setenv=_build_setenv(
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
        secret_dirs=(), secret_files=(), setenv=(), network=True, note=note,
    )


def _build_setenv(
    engine_env_result: Mapping[str, str], environ: Mapping[str, str],
    env_passthrough: tuple[str, ...], session_socket: str = "",
    shim_dir: str = "", tmpdir: str = "",
) -> tuple[tuple[str, str], ...]:
    """Env allowlist (spec B.4). IMP_* and PYTHONPATH come from engine_env_result
    (authoritative; PYTHONPATH is the single deliberate non-IMP_ exception — the
    imp-on-imp import shadow); generic + LC_* + env_passthrough pass through from
    `environ`. With the socket and every IMP_* path now identical inside and outside
    the sandbox, nothing is rewritten — real host paths pass through verbatim.

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
    if session_socket:
        d["IMP_SESSION_SOCKET"] = session_socket
    if shim_dir:
        d["PATH"] = (shim_dir + os.pathsep + d["PATH"]) if d.get("PATH") else shim_dir
    if tmpdir:
        d["TMPDIR"] = tmpdir   # per-quest scratch; last → wins over ambient/env_passthrough
    return tuple(d.items())
