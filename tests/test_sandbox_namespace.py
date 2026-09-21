"""Keeper-namespace integration tests: a real bwrap keeper, joined by nsenter.

Each test opens a namespace from a real active policy, runs real commands in it
and asserts the OS-level outcome — cross-call persistence, atomic teardown, and
the isolation the join argv must not lose. Roots live under the default temp dir,
and each gets its own rw worktree bind, so they are writable inside the namespace
and visible outside it.
"""
from __future__ import annotations

import dataclasses
import os
import select
import shutil
import signal
import subprocess
import tempfile
import time
import uuid
from pathlib import Path

import pytest

from imp.engine import sandbox as sandbox_mod
from imp.engine.sandbox import (
    HomeLayer,
    SandboxNamespace,
    SandboxOpenFailed,
    SandboxPolicy,
    make_home_layer,
    overlay_mode,
    sandbox_available,
    with_scratch_slot,
)
from tests.overlay_probe import OVERLAY_BASE, requires_overlay

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
                secret_dirs: tuple[str, ...] = (),
                quest_tmp: Path | None = None) -> SandboxPolicy:
    """A minimal real active policy whose worktree is `root`, with the host PATH so
    sh, ps, kill and readlink resolve inside the namespace. `home` defaults inside
    the worktree; the overlay test passes a sibling, which no later bind covers.
    `quest_tmp` re-exposes a scratch dir rw at its real path under a tmpfs over its
    parent — the shape resolve_policy produces — and points TMPDIR at it."""
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
            *((("TMPDIR", str(quest_tmp)),) if quest_tmp is not None else ()),
        ),
        network=network,
        note="",
        quest_tmp_dir=str(quest_tmp) if quest_tmp is not None else "",
        quest_tmp_mask=str(quest_tmp.parent) if quest_tmp is not None else "",
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
    root = Path(tempfile.mkdtemp(prefix="imp-test-nsovl-", dir=OVERLAY_BASE))
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
    root = Path(tempfile.mkdtemp(prefix="imp-test-nsmask-", dir=OVERLAY_BASE))
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
            done = _join(ns, f'pkill -f "{needle}"; echo pkill-rc=$?; ps auxww',
                         cwd=str(root))
            assert done.returncode == 0                    # not 143/137
            assert done.stdout.splitlines()[0] == "pkill-rc=1"   # no process matched
            # Sampled from inside, WHILE the shell running the command is alive: a
            # sample taken after it exits cannot tell "never on argv" from "gone".
            assert needle not in done.stdout
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


def test_join_exec_execs_the_argv_with_no_shell_and_no_command_env():
    # AC 7(a)/(b): a pty session's payload is an argv, not a script. It must be
    # exec'd directly — a shell between the tty and the program would add a
    # process and reinterpret its arguments — and the cwd rides on --wdns, which
    # resolves in the TARGET mount namespace, replacing join()'s inner `cd`.
    root = _make_root()
    try:
        ns = SandboxNamespace.open(policy=_policy_for(root), engine_env={},
                                   environ=os.environ)
        try:
            payload = ["vim", "-u", "NONE", "notes.txt"]
            spawn = ns.join_exec(payload, cwd=str(root),
                                 engine_env={"IMP_QUEST_TITLE": "Renamed quest"})
            assert spawn.argv[-len(payload):] == payload
            # No sh -c anywhere in the WRAPPER — join()'s tail carries both tokens;
            # this one must not. Scoped to the prefix: the payload may name a shell.
            head = spawn.argv[:len(spawn.argv) - len(payload)]
            assert "sh" not in head
            assert "-c" not in head
            assert f"--wdns={root}" in spawn.argv
            # --wd= (like Popen's cwd=) resolves in the CALLER's mount namespace —
            # a measured cwd escape out of a masked directory.
            assert [a for a in spawn.argv if a.startswith("--wd=")] == []
            # The capability drop join() carries, kept verbatim and immediately
            # before the payload: entering the keeper's own user namespace hands
            # CAP_FULL_SET whenever bwrap built a single userns level.
            i = spawn.argv.index("unshare")
            assert spawn.argv[i:i + 4] == ["unshare", "--user",
                                           "--map-current-user", "--"]
            assert i + 4 == len(spawn.argv) - len(payload)
            # No script to read them, and the argv is already on argv.
            assert "IMP_COMMAND" not in spawn.env
            assert "IMP_CWD" not in spawn.env
            # The env is still join()'s: policy.child_env overlaid with the LIVE
            # per-call engine_env, so a post-rename title reaches the next spawn.
            assert spawn.env["IMP_QUEST_TITLE"] == "Renamed quest"
            assert spawn.env["HOME"] == str(root / "home")
        finally:
            ns.close()
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_join_exec_enters_the_same_namespaces_as_join_by_the_same_pinned_fds():
    # The fds must ride on the spawn: Popen closes every other inherited fd, and a
    # join whose /proc/self/fd/N arguments name nothing fails ENOENT. Comparing
    # against join() is the point — the two paths must reach the SAME namespaces,
    # or a pty program would be confined somewhere the run's bash calls are not.
    root = _make_root()
    try:
        ns = SandboxNamespace.open(policy=_policy_for(root), engine_env={},
                                   environ=os.environ)
        try:
            joined = ns.join("true", cwd=str(root), engine_env={})
            execed = ns.join_exec(["true"], cwd=str(root), engine_env={})
            assert execed.pass_fds == joined.pass_fds
            assert execed.pass_fds != ()          # the pinned ns fds, not empty
            pinned = [a for a in execed.argv if "/proc/self/fd/" in a]
            assert pinned == [a for a in joined.argv if "/proc/self/fd/" in a]
            # every pinned fd is actually named, so none can be silently dropped
            assert len(pinned) == len(set(execed.pass_fds))
            assert "--preserve-credentials" in execed.argv
        finally:
            ns.close()
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_join_exec_refuses_a_closed_namespace():
    # close() shuts the pinned fds, and fd numbers are recycled: naming
    # /proc/self/fd/N afterwards would enter whatever namespace has since taken
    # that number. Fail loud instead, exactly as join() does.
    root = _make_root()
    try:
        ns = SandboxNamespace.open(policy=_policy_for(root), engine_env={},
                                   environ=os.environ)
        ns.close()
        with pytest.raises(AssertionError):
            ns.join_exec(["true"], cwd=str(root), engine_env={})
    finally:
        shutil.rmtree(root, ignore_errors=True)


def _open_fds() -> set[str]:
    """The process's currently-open fd numbers. The transient dirfd this listing
    itself opens takes the lowest free number in every snapshot, so it cancels out
    between two snapshots taken with the same fd table."""
    return set(os.listdir("/proc/self/fd"))


def test_open_pins_the_keepers_own_devpts_and_close_releases_it() -> None:
    """AC 6(b). The pin must name the KEEPER's devpts instance: a pin on imp's own
    /dev/pts would allocate ptys whose nodes do not exist inside the namespace,
    which is exactly today's broken state."""
    root = _make_root()
    try:
        ns = SandboxNamespace.open(policy=_policy_for(root), engine_env={},
                                   environ=os.environ)
        pinned = ns.devpts_fd
        try:
            got = os.fstat(pinned)
            want = os.stat(f"/proc/{ns.ns_init_pid}/root/dev/pts")
            assert (got.st_dev, got.st_ino) == (want.st_dev, want.st_ino)
            assert got.st_dev != os.stat("/dev/pts").st_dev
        finally:
            ns.close()
        with pytest.raises(OSError):
            os.fstat(pinned)
    finally:
        shutil.rmtree(root, ignore_errors=True)


@pytest.mark.parametrize("failure", [SandboxOpenFailed("boom"), OSError(5, "boom")],
                         ids=["sandbox_open_failed", "oserror"])
def test_an_open_that_fails_after_the_devpts_pin_leaks_no_fd(monkeypatch, failure) -> None:
    """AC 6(c). devpts_fd lives OUTSIDE ns_fds, so _abandon_keeper cannot close it as
    a side effect of walking that tuple — both call sites have to hand it over.
    _ns_owner_fd is the statement right after the pin, so failing it exercises the
    exact window where the fd is open and the namespace is not yet built.
    _open_once is called directly: open() would retry the whole spawn budget."""
    root = _make_root()
    try:
        def boom(fds: dict[str, int]) -> int | None:
            raise failure

        monkeypatch.setattr(sandbox_mod, "_ns_owner_fd", boom)
        before = _open_fds()
        with pytest.raises(SandboxOpenFailed):
            SandboxNamespace._open_once(policy=_policy_for(root), engine_env={},
                                        environ=os.environ)
        assert _open_fds() - before == set()
    finally:
        shutil.rmtree(root, ignore_errors=True)


def _pts_inside(ns: SandboxNamespace, cwd: str) -> set[str]:
    """What /dev/pts holds as seen from INSIDE the keeper — `ptmx` alone until
    something allocates from the namespace's own devpts."""
    done = _join(ns, "ls /dev/pts", cwd=cwd)
    assert done.returncode == 0, done.stderr
    return set(done.stdout.split())


def test_open_pty_allocates_a_working_pty_from_the_keepers_devpts() -> None:
    root = _make_root()
    try:
        ns = SandboxNamespace.open(policy=_policy_for(root), engine_env={},
                                   environ=os.environ)
        try:
            master, slave = ns.open_pty()
            try:
                assert os.isatty(master) and os.isatty(slave)
                # Provenance, and the whole point: the slave's node lives in the
                # KEEPER's devpts superblock. A pty.openpty() here would put it in
                # imp's, where the namespace cannot see it.
                assert os.fstat(slave).st_dev == os.fstat(ns.devpts_fd).st_dev
                assert os.fstat(slave).st_dev != os.stat("/dev/pts").st_dev
                # ...and it is a real line-disciplined pty, not just a pair of fds.
                os.write(master, b"ping\n")
                ready, _, _ = select.select([slave], [], [], _SIGNAL_BUDGET_S)
                assert ready, "the slave never saw what was written to the master"
                assert os.read(slave, 64) == b"ping\n"
            finally:
                os.close(slave)
                os.close(master)
        finally:
            ns.close()
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_two_allocations_get_distinct_numbers_and_closing_one_frees_it() -> None:
    """AC 6(a). The numbers are read from INSIDE the namespace, which is also the
    proof the nodes are there at all — today /dev/pts holds ptmx and nothing else."""
    root = _make_root()
    try:
        ns = SandboxNamespace.open(policy=_policy_for(root), engine_env={},
                                   environ=os.environ)
        try:
            assert _pts_inside(ns, str(root)) == {"ptmx"}
            m1, s1 = ns.open_pty()
            first = _pts_inside(ns, str(root)) - {"ptmx"}
            m2, s2 = ns.open_pty()
            try:
                both = _pts_inside(ns, str(root)) - {"ptmx"}
                assert len(first) == 1
                assert len(both) == 2 and first < both
                second = both - first
                os.close(s1)
                os.close(m1)
                _poll(lambda: _pts_inside(ns, str(root)) - {"ptmx"} == second,
                      what=f"{first} freed inside the namespace, {second} kept")
            finally:
                os.close(s2)
                os.close(m2)
        finally:
            ns.close()
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_a_failed_allocation_strands_neither_an_fd_nor_a_node(monkeypatch) -> None:
    """Opening ptmx already allocates the index and creates the slave node, before
    open_pty can learn its number — so an ioctl that fails in between must close the
    master, which is what makes the kernel take that node back. 0xDEAD is in no
    tty ioctl range, so the driver answers ENOTTY and nothing else happens."""
    root = _make_root()
    try:
        ns = SandboxNamespace.open(policy=_policy_for(root), engine_env={},
                                   environ=os.environ)
        try:
            monkeypatch.setattr(sandbox_mod, "TIOCGPTN", 0x0000DEAD)
            before = _open_fds()
            with pytest.raises(OSError):
                ns.open_pty()
            assert _open_fds() - before == set()
            _poll(lambda: _pts_inside(ns, str(root)) == {"ptmx"},
                  what="the half-allocated node reclaimed inside the namespace")
        finally:
            ns.close()
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_two_scratch_slots_cannot_see_each_others_files():
    # The isolation the slot exists for. Narrowing the RE-EXPOSE rather than handing
    # out distinct names is what makes a collision unrepresentable: the tmpfs still
    # covers the scratch root, so a sibling's dir is absent from the namespace.
    # Neither slot dir exists before its open — prepare_spawn_policy mints it.
    root = _make_root()
    scratch_root = Path(
        tempfile.mkdtemp(prefix="imp-ns-scratch-")) / f"imp-{os.getuid()}"
    quest_tmp = scratch_root / "qid"
    try:
        base = _policy_for(root, quest_tmp=quest_tmp)
        ns_a = SandboxNamespace.open(policy=with_scratch_slot(base, "explorer-1"),
                                     engine_env={}, environ=os.environ)
        try:
            ns_b = SandboxNamespace.open(
                policy=with_scratch_slot(base, "explorer-2"),
                engine_env={}, environ=os.environ)
            try:
                wrote = _join(ns_a, 'echo A > "$TMPDIR/probe" && cat "$TMPDIR/probe"',
                              cwd=str(root))
                assert wrote.returncode == 0, wrote.stderr
                assert wrote.stdout.strip() == "A"
                assert (quest_tmp / "explorer-1" / "probe").read_text() == "A\n"

                # (a) unreachable from slot B: not in its own $TMPDIR, not at A's
                # real host path, and A's slot dir is not even a directory there.
                probe_a = quest_tmp / "explorer-1" / "probe"
                seen = _join(
                    ns_b,
                    'ls -A "$TMPDIR"; '
                    f'test -e {probe_a} && echo LEAKED; '
                    f'test -d {quest_tmp / "explorer-1"} && echo SLOTVISIBLE; '
                    'echo done',
                    cwd=str(root))
                assert seen.stdout.split() == ["done"], seen.stdout

                # (b) B still has a working scratch dir of its own, and A's is intact
                mine = _join(ns_b, 'echo B > "$TMPDIR/probe" && echo ok',
                             cwd=str(root))
                assert mine.returncode == 0, mine.stderr
                assert "ok" in mine.stdout
                assert (quest_tmp / "explorer-2" / "probe").read_text() == "B\n"
                assert probe_a.read_text() == "A\n"

                # (c) host /tmp outside the scratch root is still the read-only floor
                for ns in (ns_a, ns_b):
                    denied = _join(ns, "echo x > /tmp/probe", cwd=str(root))
                    assert denied.returncode != 0, denied.stdout
            finally:
                ns_b.close()
        finally:
            ns_a.close()
    finally:
        shutil.rmtree(root, ignore_errors=True)
        shutil.rmtree(scratch_root.parent, ignore_errors=True)
