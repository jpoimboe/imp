"""Keeper-namespace integration tests: a real bwrap keeper, joined by nsenter.

Each test opens a namespace from a real active policy, runs real commands in it
and asserts the OS-level outcome — cross-call persistence, atomic teardown, and
the isolation the join argv must not lose. Roots live under the default temp dir
(/tmp), which build_sandbox_spawn binds by identity, so they are visible inside
and outside the namespace.
"""
from __future__ import annotations

import os
import shutil
import signal
import subprocess
import tempfile
import time
import uuid
from pathlib import Path

import pytest

from imp.engine.sandbox import (
    SandboxNamespace,
    SandboxPolicy,
    sandbox_available,
)

# The module timeout is the SINGLE outer backstop for a genuine hang. It has to
# exceed the sum of the inner budgets a test can spend, or an inner budget could
# never actually be reached and the two would not be independent.
pytestmark = [
    pytest.mark.skipif(not sandbox_available(), reason="bwrap unavailable"),
    pytest.mark.timeout(180),
]

_JOIN_TIMEOUT_S = 30.0     # one joined command; generous vs the measured ~2ms join
_SIGNAL_BUDGET_S = 30.0    # budget for a polled process to appear or vanish
_POLL_INTERVAL_S = 0.05

# A process that keeps its marker on argv: `sleep 300 <marker>` is rejected by GNU
# sleep, and a marker in the environment is invisible to pgrep -f.
_ALIVE = "while :; do sleep 5; done"


def _make_root() -> Path:
    return Path(tempfile.mkdtemp(prefix="imp-test-ns-"))


def _policy_for(root: Path, *, network: bool = True, home: Path | None = None,
                secret_dirs: tuple[str, ...] = ()) -> SandboxPolicy:
    """A minimal real active policy whose worktree is `root`, with the host PATH so
    sh, ps, kill and readlink resolve inside the namespace. `home` defaults inside
    the worktree; the overlay test passes a sibling, which no later bind covers."""
    git = root / ".git"
    git.mkdir(exist_ok=True)
    home = home or (root / "home")
    home.mkdir(exist_ok=True)
    return SandboxPolicy(
        active=True,
        worktree=str(root),
        git_common_dir=str(git),
        git_ro_remask=(),
        writable_caches=(),
        extra_readable=(),
        extra_writable=(),
        secret_dirs=secret_dirs,
        secret_files=(),
        child_env=(
            ("PATH", os.pathsep.join(
                p for p in os.environ.get("PATH", "/usr/bin:/bin").split(os.pathsep)
                if "imp-shim" not in p) or "/usr/bin:/bin"),
            ("HOME", str(home)),
        ),
        network=network,
        note="",
    )


def _join(ns: SandboxNamespace, command: str, *, cwd: str) -> subprocess.CompletedProcess[str]:
    spawn = ns.join(command, cwd=cwd, engine_env={})
    return subprocess.run(spawn.argv, env=spawn.env, pass_fds=spawn.pass_fds,
                          capture_output=True, text=True, timeout=_JOIN_TIMEOUT_S)


def _host_pids(marker: str) -> list[str]:
    """Host-side pids whose argv carries `marker` (pgrep matches argv, not env)."""
    p = subprocess.run(["pgrep", "-f", marker], capture_output=True, text=True)
    return p.stdout.split()


def _poll(predicate, *, what: str):
    """Poll for the actual signal rather than sleeping a fixed interval; the budget
    is generous so it cannot false-fire under a saturated -n auto run."""
    deadline = time.monotonic() + _SIGNAL_BUDGET_S
    while time.monotonic() < deadline:
        got = predicate()
        if got:
            return got
        time.sleep(_POLL_INTERVAL_S)
    raise AssertionError(f"never observed: {what}")


def test_a_joined_command_runs_in_the_keeper_and_propagates_its_exit_status():
    root = _make_root()
    try:
        ns = SandboxNamespace.open(policy=_policy_for(root), engine_env={},
                                   environ=os.environ)
        try:
            done = _join(ns, "echo hi; exit 7", cwd=str(root))
            assert (done.returncode, done.stdout.strip(), done.stderr) == (7, "hi", "")
            # the command travels in the env, so a descendant must not inherit it
            child = _join(ns, "sh -c env", cwd=str(root))
            assert "IMP_COMMAND" not in child.stdout
            assert "IMP_CWD" not in child.stdout
            # the join sees the keeper's pid namespace, not the host's
            names = _join(ns, "ps -eo comm=", cwd=str(root)).stdout.split()
            assert names[0] == "bwrap"   # the keeper is pid 1 of the namespace
            assert len(names) <= 5       # the keeper and this command, nothing else
        finally:
            ns.close()
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_open_records_the_base_policy_it_was_opened_from():
    # The reuse key must be the value open() was HANDED, not the prepared one:
    # prepare_spawn_policy prunes and mints, so a prepared policy never compares
    # equal to the caller's and would rebuild the namespace on every check.
    root = _make_root()
    try:
        policy = _policy_for(root)
        ns = SandboxNamespace.open(policy=policy, engine_env={}, environ=os.environ)
        try:
            assert ns.base_policy is policy
        finally:
            ns.close()
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_a_process_left_running_is_visible_and_killable_in_a_later_join():
    root = _make_root()
    marker = f"imp-ns-{uuid.uuid4().hex}"
    try:
        ns = SandboxNamespace.open(policy=_policy_for(root), engine_env={},
                                   environ=os.environ)
        try:
            planted = _join(
                ns, f"nohup setsid sh -c '{_ALIVE}' {marker} >/dev/null 2>&1 &",
                cwd=str(root))
            assert planted.returncode == 0

            def listed() -> str:
                out = _join(ns, f"ps -eo pid,args | grep {marker} | grep -v grep",
                            cwd=str(root)).stdout
                return out.split()[0] if out.strip() else ""

            ns_pid = _poll(listed, what=f"{marker} in a later join's ps")
            killed = _join(ns, f"kill {ns_pid}", cwd=str(root))
            assert killed.returncode == 0
            _poll(lambda: not _host_pids(marker), what=f"{marker} gone host-side")
        finally:
            ns.close()
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_close_kills_every_survivor_shape_at_once():
    root = _make_root()
    marker = f"imp-ns-{uuid.uuid4().hex}"
    plant = (
        f"sh -c '{_ALIVE}' {marker}-plain >/dev/null 2>&1 &\n"
        f"nohup setsid sh -c '{_ALIVE}' {marker}-setsid >/dev/null 2>&1 &\n"
        f"( ( sh -c '{_ALIVE}' {marker}-double >/dev/null 2>&1 & ) & )\n"
        f"""sh -c "trap '' TERM; {_ALIVE}" {marker}-trap >/dev/null 2>&1 &\n"""
    )
    try:
        ns = SandboxNamespace.open(policy=_policy_for(root), engine_env={},
                                   environ=os.environ)
        try:
            assert _join(ns, plant, cwd=str(root)).returncode == 0
            _poll(lambda: len(_host_pids(marker)) == 4,
                  what=f"four {marker} survivors alive host-side")
            ns.close()
            _poll(lambda: not _host_pids(marker),
                  what=f"zero {marker} survivors after close()")
        finally:
            ns.close()   # the second call pins idempotency, which teardown relies on
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_a_join_into_a_dead_keeper_fails_closed():
    # The pinned-fd join must FAIL when the ns-init is gone, never fall back to
    # running the command on the host — the failure mode a recycled --target pid has.
    root = _make_root()
    try:
        ns = SandboxNamespace.open(policy=_policy_for(root), engine_env={},
                                   environ=os.environ)
        try:
            os.kill(ns.ns_init_pid, signal.SIGKILL)
            _poll(lambda: ns.proc.poll() is not None, what="the keeper exiting")
            escaped = root / "escaped.txt"
            done = _join(ns, f"touch {escaped}", cwd=str(root))
            assert done.returncode != 0
            assert not escaped.exists()
        finally:
            ns.close()   # idempotent, and must not signal the now-recyclable pid
    finally:
        shutil.rmtree(root, ignore_errors=True)
