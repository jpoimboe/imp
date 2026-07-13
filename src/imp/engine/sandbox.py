"""Shell sandbox (bubblewrap) — pure policy core + argv builder (spec Part B).

Two spawn points consume this: the `bash` tool (agents/tools/bash.py) and script
gates (engine/gates.py). `resolve_policy` does the one-time stat I/O and bakes a
frozen `SandboxPolicy`; `build_sandbox_argv` is pure given that policy.

The flag ORDER in build_sandbox_argv is load-bearing: bwrap is last-wins, so the
writable-home bind (when a home_layer is present) and then the /tmp bind are
emitted EARLY (right after the ro-floor, before worktree/git/remask) and the
secret masks MUST be emitted AFTER every bind (incl. extra_writable), else
a config-driven bind could re-expose a masked secret dir (spec L1-r5). No
--unshare-pid / --die-with-parent / --new-session — all three break the runner's
interrupt grace (spec B.9); reaping is via the process group.
"""

from __future__ import annotations

import contextlib
import os
import platform
import shlex
import shutil
import subprocess
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

from imp.engine.env import HEADLESS_ENV


@dataclass(frozen=True, slots=True)
class HomeLayer:
    """The writable-home layer bound over $HOME for an active-sandbox agent.
    Fallback (rw) shape: only `home` set → a plain `--bind home home`. COW overlay
    shape: merged/upper/work set → bwrap is wrapped with unshare + mount -t
    overlay (lowerdir=home) and the merged view is bound onto `home`."""
    home: str
    merged: str = ""     # overlay mountpoint bound onto $HOME ("" → plain rw bind of home)
    upper: str = ""      # overlay upperdir (captured writes; discarded at teardown)
    work: str = ""       # overlay workdir

    @property
    def is_overlay(self) -> bool:
        return bool(self.upper)


@dataclass(frozen=True, slots=True)
class SandboxPolicy:
    active: bool                              # True → wrap with bwrap; False → bare sh -c
    worktree: str                             # rw bind at REAL path
    git_common_dir: str                       # <control>/.git rw bind
    git_ro_remask: tuple[str, ...]            # .git/hooks, .git/config, every config.worktree (ro)
    writable_caches: tuple[str, ...]          # rw toolchain cache dirs (existing only)
    extra_readable: tuple[str, ...]           # config extra_readable (ro)
    extra_writable: tuple[str, ...]           # config extra_writable (rw)
    secret_dirs: tuple[str, ...]              # --tmpfs masks (existing only) incl. /run/user/$UID
    secret_files: tuple[str, ...]             # --ro-bind /dev/null masks (existing only)
    setenv: tuple[tuple[str, str], ...]       # resolved env allowlist key/value pairs
    network: bool                             # True → no --unshare-net
    note: str = ""                            # "" | "unsandboxed_by_config" | "sandbox_disabled"
    quest_home: str = ""                     # <control>/.imp/quests/<id> rw bind ("" → none)
    readonly_overlays: tuple[str, ...] = ()  # ro subtrees; after git_ro_remask, before caches
    git_ref_reexpose: tuple[str, ...] = ()   # rw re-expose of refs/heads/imp; after git_ro_remask
    disposable_tree: bool = False            # agent runs in a disposable git worktree
    home: str = ""                           # real $HOME ("" → inactive/unresolved)
    home_layer: HomeLayer | None = None      # writable-home layer (None → ro read-through)

    def allows_read(self, path: Path) -> bool:
        """In-process mirror of the sandbox's read exposure: the whole host is
        readable (--ro-bind / /) EXCEPT the secret masks — secret_dirs as path
        prefixes, secret_files as exact paths. Deliberately stricter than bwrap
        (which serves empties for masked reads); see spec Decisions."""
        if any(path.is_relative_to(Path(d)) for d in self.secret_dirs):
            return False
        return not any(path == Path(f) for f in self.secret_files)

    def bind_plan(self) -> list[tuple[str, str]]:
        """The ordered path-covering binds, in bwrap EMIT ORDER — the SINGLE
        source of truth shared by build_sandbox_argv (which formats each entry to
        argv) and allows_write (which evaluates last-covering-wins). kind is one
        of rw / ro / mask_dir / mask_file; rw grants write. The order is
        load-bearing: the ro overlays (git_ro_remask, readonly_overlays, extra_readable)
        are NOT globally last-wins — a later writable bind re-exposes a path they
        cover — only the secret masks are unconditionally last. Add a new bind here
        and BOTH consumers pick it up; the argv-parity test enforces no drift. The
        order is load-bearing and last-wins: the home_layer rw bind goes FIRST (so
        caches/worktree/secrets layered over $HOME win), /tmp next, and the secret
        masks unconditionally LAST."""
        return [
            *([(self.home_layer.home, "rw")] if self.home_layer is not None else []),
            ("/tmp", "rw"),
            (self.worktree, "rw"),
            (self.git_common_dir, "rw"),
            *([(self.quest_home, "rw")] if self.quest_home else []),
            *[(p, "ro") for p in self.git_ro_remask],
            *[(p, "rw") for p in self.git_ref_reexpose],
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


def _is_engine_passthrough(key: str) -> bool:
    """Engine-env keys allowed through the sandbox env allowlist: every IMP_*
    var, PYTHONPATH (the imp-on-imp import shadow), and the four headless
    overrides (GIT_EDITOR / GIT_TERMINAL_PROMPT / EDITOR / VISUAL) that keep a
    sandboxed git/tool invocation non-interactive. Shared by both allowlist
    sites (the per-spawn loop in build_sandbox_argv and the policy bake in
    _build_setenv) so they cannot drift."""
    return key.startswith("IMP_") or key == "PYTHONPATH" or key in HEADLESS_ENV


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
    #    over $HOME win; /tmp goes next (before the worktree/git binds and secret
    #    masks) as the agent's scratch; secret masks go LAST.
    for bind, kind in policy.bind_plan():
        if kind == "rw":
            hl = policy.home_layer
            src = (hl.merged if (hl is not None and hl.is_overlay
                                 and bind == hl.home) else bind)
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
    # 16. the command
    argv += ["sh", "-c", command]
    hl = policy.home_layer
    if hl is not None and hl.is_overlay:
        mount = (
            f"mount -t overlay overlay -o "
            f"lowerdir={shlex.quote(hl.home)},"
            f"upperdir={shlex.quote(hl.upper)},"
            f"workdir={shlex.quote(hl.work)} {shlex.quote(hl.merged)} && "
            f"exec {shlex.join(argv)}"
        )
        return ["unshare", "--user", "--map-root-user", "--mount",
                "sh", "-c", mount]
    return argv


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
    under .imp/. Owner is the real uid, so the chmod always succeeds."""
    for root, dirs, _files in os.walk(path):
        for d in dirs:
            with contextlib.suppress(OSError):
                os.chmod(os.path.join(root, d), 0o700)
    shutil.rmtree(path, ignore_errors=True)


@lru_cache(maxsize=1)
def overlay_available(probe_base: Path) -> bool:
    """Linux + `unshare` on PATH + a one-shot unprivileged `mount -t overlay`
    inside a user namespace succeeds at `probe_base` (a real dir under `.imp/`,
    NOT /tmp — some hosts refuse an overlay upperdir on tmpfs). Cached like
    sandbox_available(); Part 2 calls this to choose the COW home vs. the rw
    fallback.

    Tests that monkeypatch subprocess.run / shutil.which / platform.system must
    call overlay_available.cache_clear() first (the result is process-global
    cached).
    """
    if platform.system() != "Linux" or shutil.which("unshare") is None:
        return False
    probe = probe_base / ".overlay-probe"
    lower, upper, work, merged = (
        probe / "lower", probe / "upper", probe / "work", probe / "merged")
    try:
        for d in (lower, upper, work, merged):
            d.mkdir(parents=True, exist_ok=True)
        r = subprocess.run(
            ["unshare", "--user", "--map-root-user", "--mount", "sh", "-c",
             f"mount -t overlay overlay -o "
             f"lowerdir={shlex.quote(str(lower))},"
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

    home = cfg.home or Path.home()
    uid = cfg.uid if cfg.uid is not None else os.getuid()

    git = control_root / ".git"
    remask = [str(git / "hooks"), str(git / "config")]
    # Every existing worktrees/*/config.worktree — incl. siblings (M1-r5).
    wt_dir = git / "worktrees"
    if wt_dir.is_dir():
        remask += [str(p / "config.worktree") for p in sorted(wt_dir.iterdir())
                   if (p / "config.worktree").exists()]

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
    # the ref-mask must cover draft agents precisely because commit_memory now
    # gives them a real (locked) commit capability that a raw bash git must not
    # bypass, whereas clean-tree tolerance for the user's pre-existing WIP is
    # unchanged.
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

    # ro overlay of workflow snapshot — bind_plan emits it after the quest_home rw
    # bind, so last-covering-wins makes only workflow/ read-only.
    readonly_overlays: tuple[str, ...] = ()
    if quest_dir is not None and (quest_dir / "workflow").is_dir():
        readonly_overlays = (str(quest_dir / "workflow"),)

    return SandboxPolicy(
        active=True,
        worktree=str(worktree),
        git_common_dir=str(git),
        quest_home=str(quest_dir) if quest_dir is not None else "",
        git_ro_remask=tuple(remask),
        git_ref_reexpose=git_ref_reexpose,
        disposable_tree=disposable_tree,
        home=str(home),
        readonly_overlays=readonly_overlays,
        writable_caches=caches,
        extra_readable=tuple(cfg.extra_readable),
        extra_writable=tuple(cfg.extra_writable),
        secret_dirs=tuple(secret_dirs),
        secret_files=tuple(secret_files),
        setenv=_build_setenv(engine_env_result, environ, cfg.env_passthrough),
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
    env_passthrough: tuple[str, ...],
) -> tuple[tuple[str, str], ...]:
    """Env allowlist (spec B.4). IMP_* and PYTHONPATH come from engine_env_result
    (authoritative, M2-r5; PYTHONPATH is the single deliberate non-IMP_ exception
    — the imp-on-imp import shadow); generic + LC_* + env_passthrough pass through
    from `environ`; TMPDIR=/tmp. Ambient non-engine IMP_* vars are NOT passed
    through — the only IMP_* an agent sees is the engine-managed set from
    engine_env_result, plus anything a user explicitly names in env_passthrough.

    Dedup: accumulated into a dict (insertion-ordered); re-assigning a key updates
    its value in place (first-seen position, last value wins). TMPDIR is assigned
    last so the forced /tmp override beats any ambient or env_passthrough value.
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
    d["TMPDIR"] = "/tmp"   # forced last — wins over any ambient/env_passthrough value
    return tuple(d.items())
