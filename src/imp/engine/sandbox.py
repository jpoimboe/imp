"""Shell sandbox (bubblewrap) — pure policy core + argv builder (spec Part B).

Two spawn points consume this: the `bash` tool (agents/tools/bash.py) and script
gates (engine/gates.py). `resolve_policy` does the one-time stat I/O and bakes a
frozen `SandboxPolicy`; `build_sandbox_argv` is pure given that policy.

The flag ORDER in build_sandbox_argv is load-bearing: bwrap is last-wins, so the
secret masks MUST be emitted AFTER every bind (incl. extra_writable), else a
config-driven bind could re-expose a masked secret dir (spec L1-r5). No
--unshare-pid / --die-with-parent / --new-session — all three break the runner's
interrupt grace (spec B.9); reaping is via the process group.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class SandboxPolicy:
    active: bool                              # True → wrap with bwrap; False → bare sh -c
    worktree: str                             # rw bind at REAL path
    git_common_dir: str                       # <control>/.git rw bind
    git_ro_remask: tuple[str, ...]            # .git/hooks, .git/config, every config.worktree (ro)
    writable_caches: tuple[str, ...]          # rw toolchain cache dirs (existing only)
    extra_readable: tuple[str, ...]           # config extra_readable (ro)
    extra_writable: tuple[str, ...]           # config extra_writable (rw)
    tmp_host_dir: str                         # persistent per-change dir bound at /tmp
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
    # 2-3. worktree + git db writable (real paths)
    argv += ["--bind", policy.worktree, policy.worktree]
    argv += ["--bind", policy.git_common_dir, policy.git_common_dir]
    # 4. re-mask git escape vectors RO (hooks/config/config.worktree incl. siblings)
    for p in policy.git_ro_remask:
        argv += ["--ro-bind", p, p]
    # 5. writable toolchain caches
    for c in policy.writable_caches:
        argv += ["--bind", c, c]
    # 6-7. config extra binds (readable then writable)
    for r in policy.extra_readable:
        argv += ["--ro-bind", r, r]
    for w in policy.extra_writable:
        argv += ["--bind", w, w]
    # 8. persistent per-change /tmp
    argv += ["--bind", policy.tmp_host_dir, "/tmp"]
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
