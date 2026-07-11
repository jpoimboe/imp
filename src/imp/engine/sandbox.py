"""Shell sandbox (bubblewrap) — pure policy core + argv builder (spec Part B).

Two spawn points consume this: the `bash` tool (agents/tools/bash.py) and script
gates (engine/gates.py). `resolve_policy` does the one-time stat I/O and bakes a
frozen `SandboxPolicy`; `build_sandbox_argv` is pure given that policy.

The flag ORDER in build_sandbox_argv is load-bearing: bwrap is last-wins, so the
/tmp bind is emitted EARLY (right after the ro-floor, before worktree/git/remask)
and the secret masks MUST be emitted AFTER every bind (incl. extra_writable), else
a config-driven bind could re-expose a masked secret dir (spec L1-r5). No
--unshare-pid / --die-with-parent / --new-session — all three break the runner's
interrupt grace (spec B.9); reaping is via the process group.
"""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

from imp.engine.env import HEADLESS_ENV


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
    change_home: str = ""                     # <control>/.imp/changes/<id> rw bind ("" → none)
    readonly_overlays: tuple[str, ...] = ()  # ro subtrees; after git_ro_remask, before caches
    git_ref_reexpose: tuple[str, ...] = ()   # rw re-expose of refs/heads/imp; after git_ro_remask
    ephemeral_home: str = ""                 # spike: writable tmpfs over $HOME ("" → none)
    cred_writethrough: tuple[str, ...] = ()  # spike: rw write-through binds of provider cred dirs

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
        of rw / tmpfs_rw / ro / mask_dir / mask_file; rw and tmpfs_rw grant write
        (tmpfs_rw is the spike's writable ephemeral home — same --tmpfs argv as a
        secret mask_dir but the opposite write verdict). The order is
        load-bearing: the ro overlays (git_ro_remask, readonly_overlays, extra_readable)
        are NOT globally last-wins — a later writable bind re-exposes a path they
        cover — only the secret masks are unconditionally last. Add a new bind here
        and BOTH consumers pick it up; the argv-parity test enforces no drift."""
        return [
            ("/tmp", "rw"),
            *([(self.ephemeral_home, "tmpfs_rw")] if self.ephemeral_home else []),
            (self.worktree, "rw"),
            (self.git_common_dir, "rw"),
            *([(self.change_home, "rw")] if self.change_home else []),
            *[(p, "ro") for p in self.git_ro_remask],
            *[(p, "rw") for p in self.git_ref_reexpose],
            *[(p, "ro") for p in self.readonly_overlays],
            *[(c, "rw") for c in self.writable_caches],
            *[(r, "ro") for r in self.extra_readable],
            *[(w, "rw") for w in self.extra_writable],
            *[(c, "rw") for c in self.cred_writethrough],
            *[(d, "mask_dir") for d in self.secret_dirs],
            *[(f, "mask_file") for f in self.secret_files],
        ]

    def allows_write(self, path: Path) -> bool:
        """In-process mirror of the sandbox's write exposure. Walks bind_plan()
        in emit order; the LAST bind covering `path` decides. Writable iff that
        last bind is rw or tmpfs_rw (the spike's writable ephemeral home); a
        read-only overlay (git_ro_remask, extra_readable), a secret mask, or no
        covering bind denies."""
        verdict: str | None = None
        for bind, kind in self.bind_plan():
            if path.is_relative_to(Path(bind)):
                verdict = kind
        return verdict in ("rw", "tmpfs_rw")


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
    once per run (the stat-once design), but name-on-intent makes IMP_CHANGE_TITLE
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
    #    /tmp goes EARLY (right after the ro floor) so the later worktree/git binds,
    #    git RO-remask, and secret masks all win over it — a late /tmp bind would
    #    re-expose .git escape vectors for a repo under /tmp; secret masks go LAST.
    #    /tmp is the agent's scratch, like a normal shell.
    for bind, kind in policy.bind_plan():
        if kind == "rw":
            argv += ["--bind", bind, bind]
        elif kind == "ro":
            argv += ["--ro-bind", bind, bind]
        elif kind == "mask_dir":
            argv += ["--tmpfs", bind]
        elif kind == "tmpfs_rw":
            argv += ["--tmpfs", bind]   # spike ephemeral home: mask-identical argv, writable
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
    extra_env: tuple[str, ...] = ()
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
        extra_env=tuple(raw.get("extra_env", ())),
    )


def resolve_policy(
    *, worktree: Path, control_root: Path, change_id: str, cfg: SandboxConfig,
    engine_env_result: Mapping[str, str], environ: Mapping[str, str],
    available: bool, change_dir: Path | None = None, spike: bool = False,
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
    # change/spike branches still update. bind_plan() emits the re-expose after
    # the refs/heads mask, so last-wins keeps the imp/ subtree writable while
    # master and every other branch stay read-only. Existence-gated like caches/
    # secrets. Skipped for the draft/degenerate case where the agent legitimately
    # IS the control root (mirrors _enforce_clean_tree's project_root ==
    # control_root guard).
    git_ref_reexpose: tuple[str, ...] = ()
    if worktree != control_root:
        refs_heads = git / "refs" / "heads"
        remask += list(_existing(
            [str(refs_heads), str(git / "refs" / "stash"), str(git / "index")]))
        imp_refs = refs_heads / "imp"
        imp_refs.mkdir(parents=True, exist_ok=True)
        git_ref_reexpose = (str(imp_refs),)

    if spike:
        caches: tuple[str, ...] = ()
    else:
        cache_names = cfg.writable_caches or _DEFAULT_CACHES
        caches = _existing([str(home / c) for c in cache_names])

    # Secret dirs/files: code defaults + config secret_paths (each classified
    # dir-vs-file by an actual stat); + /run/user/$UID. Under a spike tmpfs home
    # the home-relative masks are moot (they live inside the now-empty tmpfs) and
    # are omitted; absolute secret_paths and /run/user/$UID are outside $HOME and
    # stay masked.
    if spike:
        secret_dirs: list[str] = []
        secret_files: list[str] = []
    else:
        secret_dirs = list(_existing([str(home / d) for d in _DEFAULT_SECRET_DIRS]))
        secret_files = list(_existing([str(home / f) for f in _DEFAULT_SECRET_FILES]))
    for extra in cfg.secret_paths:
        is_abs = extra.startswith("/")
        if spike and not is_abs:
            continue   # home-relative mask is moot under the tmpfs home
        p = Path(extra) if is_abs else (home / extra)
        if p.is_dir():
            secret_dirs.append(str(p))
        elif p.exists():
            secret_files.append(str(p))
    secret_dirs.append(f"/run/user/{uid}")   # H2 — agent sockets; mask even if abs

    ephemeral_home = ""
    cred_writethrough: tuple[str, ...] = ()
    if spike:
        ephemeral_home = str(home)
        claude_override = environ.get("CLAUDE_CONFIG_DIR")
        claude_dir = Path(claude_override) if claude_override else home / ".claude"
        cred_writethrough = _existing([str(claude_dir), str(home / ".codex")])

    return SandboxPolicy(
        active=True,
        worktree=str(worktree),
        git_common_dir=str(git),
        change_home=str(change_dir) if change_dir is not None else "",
        git_ro_remask=tuple(remask),
        git_ref_reexpose=git_ref_reexpose,
        writable_caches=caches,
        extra_readable=tuple(cfg.extra_readable),
        extra_writable=tuple(cfg.extra_writable),
        secret_dirs=tuple(secret_dirs),
        secret_files=tuple(secret_files),
        setenv=_build_setenv(engine_env_result, environ, cfg.extra_env),
        network=cfg.network,
        note="",
        ephemeral_home=ephemeral_home,
        cred_writethrough=cred_writethrough,
    )


def _inactive(*, note: str) -> SandboxPolicy:
    return SandboxPolicy(
        active=False, worktree="", git_common_dir="", git_ro_remask=(),
        writable_caches=(), extra_readable=(), extra_writable=(),
        secret_dirs=(), secret_files=(), setenv=(), network=True, note=note,
    )


def _build_setenv(
    engine_env_result: Mapping[str, str], environ: Mapping[str, str],
    extra_env: tuple[str, ...],
) -> tuple[tuple[str, str], ...]:
    """Env allowlist (spec B.4). IMP_* and PYTHONPATH come from engine_env_result
    (authoritative, M2-r5; PYTHONPATH is the single deliberate non-IMP_ exception
    — the imp-on-imp import shadow); generic + LC_* + extra_env pass through from
    `environ`; TMPDIR=/tmp.

    Dedup: accumulated into a dict (insertion-ordered); re-assigning a key updates
    its value in place (first-seen position, last value wins). TMPDIR is assigned
    last so the forced /tmp override beats any ambient or extra_env value.
    """
    d: dict[str, str] = {}
    for k in _GENERIC_ENV:
        if k in environ:
            d[k] = environ[k]
    for k, v in environ.items():
        if k.startswith("LC_"):
            d[k] = v
    for k in extra_env:
        if k in environ:
            d[k] = environ[k]
    for k, v in engine_env_result.items():
        if _is_engine_passthrough(k):
            d[k] = v
    d["TMPDIR"] = "/tmp"   # forced last — wins over any ambient/extra_env value
    return tuple(d.items())
