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


def build_sandbox_argv(command: str, *, cwd: str, policy: SandboxPolicy) -> list[str]:
    """Return the argv to spawn `command`. When the policy is active, wrap with
    bwrap per spec §B.2 (fixed flag order); otherwise the bare ["sh","-c",cmd]."""
    if not policy.active:
        return ["sh", "-c", command]

    argv: list[str] = ["bwrap"]
    # 1. whole host readable
    argv += ["--ro-bind", "/", "/"]
    # 2. real host /tmp, writable — emitted EARLY (right after the ro floor) so the
    #    later worktree/git binds, git RO-remask, and secret masks all win over it
    #    (bwrap is last-wins). A late /tmp bind would re-expose .git escape vectors
    #    for a repo under /tmp. /tmp is the agent's scratch, like a normal shell.
    argv += ["--bind", "/tmp", "/tmp"]
    # 3-4. worktree + git db writable (real paths)
    argv += ["--bind", policy.worktree, policy.worktree]
    argv += ["--bind", policy.git_common_dir, policy.git_common_dir]
    # 5. re-mask git escape vectors RO (hooks/config/config.worktree incl. siblings)
    for p in policy.git_ro_remask:
        argv += ["--ro-bind", p, p]
    # 6. writable toolchain caches
    for c in policy.writable_caches:
        argv += ["--bind", c, c]
    # 7-8. config extra binds (readable then writable)
    for r in policy.extra_readable:
        argv += ["--ro-bind", r, r]
    for w in policy.extra_writable:
        argv += ["--bind", w, w]
    # 9-10. secret masks — LAST, AFTER every bind (last-wins; spec L1-r5)
    for d in policy.secret_dirs:
        argv += ["--tmpfs", d]
    for f in policy.secret_files:
        argv += ["--ro-bind", "/dev/null", f]
    # 11. proc/dev
    argv += ["--proc", "/proc", "--dev", "/dev"]
    # 12. chdir
    argv += ["--chdir", cwd]
    # 13. env allowlist
    argv += ["--clearenv"]
    for k, v in policy.setenv:
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


def resolve_policy(
    *, worktree: Path, control_root: Path, change_id: str, cfg: SandboxConfig,
    engine_env_result: Mapping[str, str], environ: Mapping[str, str],
    available: bool,
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

    cache_names = cfg.writable_caches or _DEFAULT_CACHES
    caches = _existing([str(home / c) for c in cache_names])

    # Secret dirs/files: code defaults + config secret_paths (each classified
    # dir-vs-file by an actual stat — p.is_dir() / p.exists()); + /run/user/$UID.
    secret_dirs = list(_existing([str(home / d) for d in _DEFAULT_SECRET_DIRS]))
    secret_files = list(_existing([str(home / f) for f in _DEFAULT_SECRET_FILES]))
    for extra in cfg.secret_paths:
        p = (home / extra) if not extra.startswith("/") else Path(extra)
        if p.is_dir():
            secret_dirs.append(str(p))
        elif p.exists():
            secret_files.append(str(p))
    secret_dirs.append(f"/run/user/{uid}")   # H2 — agent sockets; mask even if abs

    return SandboxPolicy(
        active=True,
        worktree=str(worktree),
        git_common_dir=str(git),
        git_ro_remask=tuple(remask),
        writable_caches=caches,
        extra_readable=tuple(cfg.extra_readable),
        extra_writable=tuple(cfg.extra_writable),
        secret_dirs=tuple(secret_dirs),
        secret_files=tuple(secret_files),
        setenv=_build_setenv(engine_env_result, environ, cfg.extra_env),
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
    extra_env: tuple[str, ...],
) -> tuple[tuple[str, str], ...]:
    """Env allowlist (spec B.4). IMP_* come from engine_env_result (authoritative,
    M2-r5); generic + LC_* + extra_env pass through from `environ`; TMPDIR=/tmp.

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
        if k.startswith("IMP_"):
            d[k] = v
    d["TMPDIR"] = "/tmp"   # forced last — wins over any ambient/extra_env value
    return tuple(d.items())
