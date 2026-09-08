"""Keeper-namespace integration tests: a real bwrap keeper, joined by nsenter.

Each test opens a namespace from a real active policy, runs real commands in it
and asserts the OS-level outcome — cross-call persistence, atomic teardown, and
the isolation the join argv must not lose. Roots live under the default temp dir
(/tmp), which build_sandbox_spawn binds by identity, so they are visible inside
and outside the namespace.
"""
from __future__ import annotations

import dataclasses
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
    HomeLayer,
    SandboxNamespace,
    SandboxOpenFailed,
    SandboxPolicy,
    make_home_layer,
    overlay_mode,
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


_OVERLAY_BASE = Path("/var/tmp")


def _probe_overlay() -> bool | None:
    """overlay_mode's tri-state at _OVERLAY_BASE: None → no unprivileged overlay
    here; True/False → the mount shape this host accepts."""
    if not sandbox_available() or not _OVERLAY_BASE.is_dir():
        return None
    try:
        probe = Path(tempfile.mkdtemp(prefix="imp-nsovl-probe-", dir=_OVERLAY_BASE))
        home = Path(tempfile.mkdtemp(prefix="imp-nsovl-home-", dir=_OVERLAY_BASE))
    except OSError:
        return None
    try:
        return overlay_mode(probe, home)
    finally:
        shutil.rmtree(probe, ignore_errors=True)
        shutil.rmtree(home, ignore_errors=True)


requires_overlay = pytest.mark.skipif(
    _probe_overlay() is None,
    reason="sandbox unavailable, or the unprivileged overlayfs probe at /var/tmp "
           "failed (read-only base or stacked overlay under a nested sandbox)")


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


def _join(ns: SandboxNamespace, command: str, *, cwd: str, stdin: str | None = None,
          engine_env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    spawn = ns.join(command, cwd=cwd, engine_env=engine_env or {})
    return subprocess.run(spawn.argv, env=spawn.env, pass_fds=spawn.pass_fds,
                          input=stdin, capture_output=True, text=True,
                          timeout=_JOIN_TIMEOUT_S)


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
            # 128+SIGKILL proves the ns-init died of close()'s own signal: bwrap's
            # monitor propagates it, where the 10s reap fallback SIGKILLs the
            # monitor itself and shows -9.
            assert ns.proc.returncode == 128 + signal.SIGKILL
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


def test_an_isolated_keeper_does_not_leave_the_join_in_the_host_network():
    # Fails OPEN if the --net pin is dropped: the command silently keeps the HOST
    # network namespace, so this is asserted directly rather than inferred.
    root = _make_root()
    try:
        ns = SandboxNamespace.open(policy=_policy_for(root, network=False),
                                   engine_env={}, environ=os.environ)
        try:
            seen = _join(ns, "readlink /proc/self/ns/net", cwd=str(root))
            assert seen.stdout.strip() != os.readlink("/proc/self/ns/net")
            links = _join(ns, "grep -c ':' /proc/net/dev", cwd=str(root))
            assert links.stdout.strip() == "1"       # loopback only
        finally:
            ns.close()
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_a_networked_keeper_leaves_the_join_in_the_callers_network():
    root = _make_root()
    try:
        ns = SandboxNamespace.open(policy=_policy_for(root, network=True),
                                   engine_env={}, environ=os.environ)
        try:
            seen = _join(ns, "readlink /proc/self/ns/net", cwd=str(root))
            assert seen.stdout.strip() == os.readlink("/proc/self/ns/net")
        finally:
            ns.close()
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_the_join_resolves_its_cwd_in_the_keepers_mount_namespace():
    # The --wd trap: --wd resolves the path in the CALLER's mount namespace, which
    # gave a joined shell aimed at a --tmpfs mask a cwd showing the host's real
    # contents. The inner `cd` must resolve inside the namespace, showing the mask.
    root = _make_root()
    masked = root / "masked"
    masked.mkdir()
    (masked / "creds").write_text("SUPER_SECRET_KEY\n")
    try:
        ns = SandboxNamespace.open(
            policy=_policy_for(root, secret_dirs=(str(masked),)),
            engine_env={}, environ=os.environ)
        try:
            listing = _join(ns, "pwd; ls -A .", cwd=str(masked))
            lines = listing.stdout.splitlines()
            assert lines[0] == str(masked)   # the inner cd landed in the masked dir
            assert lines[1:] == []           # the empty tmpfs, not the host's `creds`
            # and the file is unreachable by name, so nothing leaks by another route.
            # `cat` is NOT redirected: its own diagnostic names the file, which would
            # put the needle in stdout and defeat the assertion.
            leaked = _join(ns, "cat creds", cwd=str(masked))
            assert leaked.returncode != 0
            assert "SUPER_SECRET_KEY" not in leaked.stdout
        finally:
            ns.close()
    finally:
        shutil.rmtree(root, ignore_errors=True)


@pytest.mark.timeout(300)   # 40 real bwrap spawns; slow under a saturated -n auto
def test_the_readiness_handshake_holds_over_forty_open_join_cycles():
    # Joining on the --info-fd child pid alone hit `nsenter: failed to execute sh:
    # No such file or directory` in 29 of 40 cycles; gating on the sentinel is
    # what makes this zero, so an empty stderr is the assertion that matters.
    root = _make_root()
    policy = _policy_for(root)
    try:
        for cycle in range(40):
            ns = SandboxNamespace.open(policy=policy, engine_env={},
                                       environ=os.environ)
            try:
                done = _join(ns, "echo ok", cwd=str(root))
                assert (done.returncode, done.stdout.strip(), done.stderr) == (
                    0, "ok", ""), f"cycle {cycle}"
            finally:
                ns.close()
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_an_unmountable_overlay_fails_the_open_after_the_whole_budget():
    # The overlay mount runs before the exec and exits nonzero, so the sentinel
    # never arrives — its absence is the sole failure signal, and no command runs.
    root = _make_root()
    policy = dataclasses.replace(
        _policy_for(root),
        home_layer=HomeLayer(home=str(root / "home"),
                             merged=str(root / "absent-merged"),
                             upper=str(root / "absent-upper"),
                             work=str(root / "absent-work")))
    seen: list[tuple[int, str]] = []
    try:
        with pytest.raises(SandboxOpenFailed) as exc:
            SandboxNamespace.open(policy=policy, engine_env={}, environ=os.environ,
                                  on_retry=lambda n, detail: seen.append((n, detail)))
        assert [n for n, _ in seen] == [2, 3, 4, 5]     # one per failed attempt
        assert exc.value.detail != ""                   # what the sandbox said
        assert all(detail for _, detail in seen)
    finally:
        shutil.rmtree(root, ignore_errors=True)


@requires_overlay
def test_a_keeper_opened_from_an_overlay_policy_owns_the_pid_namespace():
    # The production shape: build_sandbox_spawn wraps the whole bwrap argv in
    # `unshare --user --map-root-user --mount sh -c 'mount -t overlay …; exec bwrap …'`.
    # Opening at all proves --info-fd survived both extra execs as an inherited fd;
    # the pid-namespace assertion proves child-pid named the INNER bwrap's child.
    # Pinning the outer `unshare` instead also "works" — it just has no pid namespace,
    # so every joined command would quietly get the HOST process table. Fails open.
    root = Path(tempfile.mkdtemp(prefix="imp-test-nsovl-", dir=_OVERLAY_BASE))
    home = root / "home"
    home.mkdir()
    (home / "lower.txt").write_text("from-the-real-home\n")
    worktree = root / "wt"          # a SIBLING of home: no later bind may cover it
    worktree.mkdir()
    layer, overlay_root, cleanup = make_home_layer(
        str(home), root=root / "ovl", userxattr=overlay_mode(root / "probe", home))
    policy = dataclasses.replace(
        _policy_for(worktree, home=home), home=str(home), home_layer=layer,
        overlay_root=overlay_root)
    try:
        ns = SandboxNamespace.open(policy=policy, engine_env={}, environ=os.environ)
        try:
            names = _join(ns, "ps -eo comm=", cwd=str(worktree)).stdout.split()
            assert names[0] == "bwrap"   # the keeper is pid 1 of the joined namespace
            assert len(names) <= 5       # the keeper and this command — not the host
            # one layer per NAMESPACE, so a $HOME write survives into the next join
            wrote = _join(ns, "echo written-in-A > $HOME/new.txt; cat $HOME/lower.txt",
                          cwd=str(worktree))
            assert wrote.stdout == "from-the-real-home\n"    # merged view, not an empty upper
            again = _join(ns, "cat $HOME/new.txt", cwd=str(worktree))
            assert again.stdout == "written-in-A\n"
            # and it landed in the overlay, leaving the real home untouched
            assert not (home / "new.txt").exists()
        finally:
            ns.close()
    finally:
        cleanup()
        shutil.rmtree(root, ignore_errors=True)


@requires_overlay
def test_a_joined_command_cannot_unmask_a_secret_dir():
    # An overlay policy is what makes this test honest, not incidental: it runs
    # bwrap under `unshare --map-root-user`, so bwrap needs only ONE user namespace
    # and the join lands as uid 0 in it with full capabilities — the production
    # shape, and the only one where the bypass exists. The outer_user_fd assertion
    # holds the test to that shape: with two levels the caps are already empty and
    # this would pass vacuously.
    root = Path(tempfile.mkdtemp(prefix="imp-test-nsmask-", dir=_OVERLAY_BASE))
    home = root / "home"
    home.mkdir()
    worktree = root / "wt"
    worktree.mkdir()
    masked = root / "secrets"
    masked.mkdir()
    (masked / "creds").write_text("SUPER_SECRET_KEY\n")
    layer, overlay_root, cleanup = make_home_layer(
        str(home), root=root / "ovl", userxattr=overlay_mode(root / "probe", home))
    policy = dataclasses.replace(
        _policy_for(worktree, home=home, secret_dirs=(str(masked),)),
        home=str(home), home_layer=layer, overlay_root=overlay_root)
    try:
        ns = SandboxNamespace.open(policy=policy, engine_env={}, environ=os.environ)
        try:
            assert ns.outer_user_fd is None      # the one-userns shape, or nothing is proven
            done = _join(ns, f"umount {masked}; cat {masked}/creds", cwd=str(worktree))
            assert done.returncode != 0
            assert "SUPER_SECRET_KEY" not in done.stdout
            # and the mask is still a mask for the next command in the same keeper
            assert _join(ns, f"ls -A {masked}", cwd=str(worktree)).stdout == ""
        finally:
            ns.close()
    finally:
        cleanup()
        shutil.rmtree(root, ignore_errors=True)


def test_a_joined_command_cannot_pkill_the_shell_running_it():
    # AC 10a. This matters MORE under a shared namespace: a self-matching pattern
    # can now reach a sibling call's shell and every background task in the run,
    # not just the agent's own shell. The needle occurs only inside the command.
    root = _make_root()
    needle = f"imp-needle-{uuid.uuid4().hex}"
    try:
        ns = SandboxNamespace.open(policy=_policy_for(root), engine_env={},
                                   environ=os.environ)
        try:
            done = _join(ns, f'pkill -f "{needle}"; echo pkill-rc=$?', cwd=str(root))
            assert done.returncode == 0                    # not 143/137
            assert done.stdout.strip() == "pkill-rc=1"     # no process matched
            listing = subprocess.run(["ps", "auxww"], capture_output=True,
                                     text=True).stdout
            assert needle not in listing
        finally:
            ns.close()
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_the_joined_shell_matches_a_plain_sh_c():
    # AC 11a/b: the eval shape is semantically identical to `sh -c <command>` —
    # status propagation, set -e, $0/$#/$@, and the command's own stdin, which
    # `sh -s` would have consumed as the script pipe. As on the bare shape, the
    # "/dev/null when nothing is piped" half belongs to the spawn site, not to
    # the script, so it is not what this pins.
    root = _make_root()
    try:
        ns = SandboxNamespace.open(policy=_policy_for(root), engine_env={},
                                   environ=os.environ)
        try:
            assert _join(ns, "exit 7", cwd=str(root)).returncode == 7
            assert _join(ns, "true; false", cwd=str(root)).returncode == 1
            aborted = _join(ns, "set -e; echo hello; false; echo unreachable",
                            cwd=str(root))
            assert (aborted.returncode, aborted.stdout) == (1, "hello\n")
            assert _join(ns, 'echo "[$0] [$#] [$@]"',
                         cwd=str(root)).stdout == "[sh] [0] []\n"
            piped = _join(ns, "cat", cwd=str(root), stdin="piped-in\n")
            assert (piped.returncode, piped.stdout) == (0, "piped-in\n")
        finally:
            ns.close()
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_a_later_join_sees_an_engine_value_that_changed_mid_run():
    # AC 11d: the child env is rebuilt per join, never captured at open(), so a
    # post-rename IMP_QUEST_TITLE beats the value frozen into policy.child_env at
    # run start (name-on-intent). Reusing the open()-time env would regress it
    # silently — the keeper would still work, just with a stale title.
    root = _make_root()
    show = "echo ${IMP_QUEST_TITLE:-unset}"
    try:
        ns = SandboxNamespace.open(policy=_policy_for(root), engine_env={},
                                   environ=os.environ)
        try:
            before = _join(ns, show, cwd=str(root),
                           engine_env={"IMP_QUEST_TITLE": ""})
            after = _join(ns, show, cwd=str(root),
                          engine_env={"IMP_QUEST_TITLE": "Renamed quest"})
            assert before.stdout.strip() == "unset"
            assert after.stdout.strip() == "Renamed quest"
        finally:
            ns.close()
    finally:
        shutil.rmtree(root, ignore_errors=True)
