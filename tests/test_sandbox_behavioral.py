"""Behavioral bwrap integration tests (spec Part B §7 / B10).

Each test builds a REAL active SandboxPolicy, runs a real bash command under
bwrap, and asserts the OS-level outcome (write-confinement, secret masking,
/tmp persistence, network argv, git commit + escape-vector blocking).

Sandbox worktrees use the default temp directory (/tmp). /tmp is bound rw to
the real host /tmp (identity bind, right after the ro-floor). A worktree under
/tmp is NOT shadowed — --chdir works and /tmp is visible inside and outside the
sandbox.
"""
from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import json
import os
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace

import anyio
import pytest

import imp.agents.tools.bash  # noqa: F401 — registers the tool
from imp import __version__
from imp.agents.runner import QuestContext
from imp.agents.tools import get_registry
from imp.agents.tools.bg import terminate_background_processes
from imp.agents.tools.context import (
    ToolContext,
    attach_impty_driver,
    ensure_tool_namespace,
    make_impty_sandbox_hook,
    teardown_tool_context,
    terminate_running_processes,
)
from imp.engine import worktree as wt
from imp.engine.sandbox import (
    SandboxConfig,
    SandboxNamespace,
    SandboxPolicy,
    build_sandbox_spawn,
    make_home_layer,
    overlay_mode,
    resolve_policy,
    sandbox_available,
)
from imp.engine.shim import write_imp_shim
from imp.paths import imp_home, project_state_dir
from impty import TerminalSession
from impty.driver import ImptyDriver
from tests.conftest import assert_all_tool_contexts_owned, own_tool_context
from tests.impty.e2e_support import can_pty
from tests.test_sandbox_namespace import _host_pids, _poll

pytestmark = pytest.mark.skipif(
    not sandbox_available(),
    reason="bwrap unavailable",
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_root(prefix: str = "imp-test-beh-") -> Path:
    """Create a sandbox root under the default temp dir (/tmp).

    The worktree is bound rw after the /tmp bind, so it is not shadowed inside
    the namespace — no /var/tmp workaround needed.
    """
    return Path(tempfile.mkdtemp(prefix=prefix))


def _active_policy(worktree: Path, *, home: Path | None = None,
                   secret_dirs: tuple[str, ...] = ()) -> SandboxPolicy:
    """Minimal hand-built active policy for simple behavioral tests.

    Uses PATH from the real environment so git, cat, echo, etc. resolve.
    HOME is set to a dir under the worktree root (or the provided `home`).
    This hand-built policy sets no /tmp source, so /tmp is bound rw by identity in
    build_sandbox_spawn — no per-test tmp dir setup is needed.
    """
    git = worktree / ".git"
    git.mkdir(exist_ok=True)
    _home = home or (worktree / "home")
    _home.mkdir(exist_ok=True)

    return SandboxPolicy(
        active=True,
        worktree=str(worktree),
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
            ("HOME", str(_home)),
        ),
        network=True,
        note="",
    )


def _ctx(worktree: Path, policy: SandboxPolicy) -> ToolContext:
    ctx = ToolContext(
        project_root=worktree,
        events_path=worktree / "events.jsonl",
    )
    ctx.quest_context = QuestContext(
        title="Test",
        project_root=worktree,
        workflow_dir=worktree / ".imp" / "wf",
    )
    ctx.base_sandbox_policy = policy
    return own_tool_context(ctx)


def _bash(ctx: ToolContext, cmd: str) -> dict[str, object]:
    """Run cmd under ctx's sandbox policy; return parsed JSON {stdout, stderr, returncode}."""
    pol = getattr(ctx, "base_sandbox_policy", None)
    if pol is not None and getattr(pol, "quest_var_tmp_dir", ""):
        # The /var/tmp bind SOURCE: the executor provisions it before real spawns;
        # direct-resolve tests must create it here or bwrap aborts on the missing
        # --bind source. No-op when quest_var_tmp_dir == "".
        Path(pol.quest_var_tmp_dir).mkdir(parents=True, exist_ok=True)
    async def _run() -> str:
        return await get_registry()["bash"].async_invoke(ctx, {"command": cmd})
    return json.loads(anyio.run(_run))


# The overlay home under test must live at a path covered by NO later bind_plan
# bind — NOT under /tmp (build_sandbox_spawn emits --bind /tmp /tmp, which re-mounts
# the host /tmp OVER an overlay home placed under /tmp, defeating it) and NOT under
# the sandbox worktree (its rw bind does the same). The home-layer rw bind is
# emitted FIRST (sandbox.py:91), so it can never be the last-covering bind for a
# path that a later bind also covers. So the overlay tests root their home AND
# worktree as SIBLINGS under a real fs OUTSIDE /tmp (/var/tmp): the --ro-bind / /
# floor keeps them readable, and the home-layer rw bind is then the ONLY bind
# covering the home path, so the overlay isolates correctly (writes captured in the
# upper; real home untouched). /var/tmp is also a real disk fs — a valid overlay
# upperdir location, unlike a possible tmpfs /tmp.
_OVERLAY_BASE = Path("/var/tmp")


def _probe_overlay_at_base() -> bool | None:
    """Probe overlay support at the /var/tmp base, removing the probe dirs after,
    and return overlay_mode's tri-state: None → no unprivileged overlay here (for
    ANY reason, including no bwrap and no /var/tmp); True/False → the mount shape
    this host accepts. overlay_mode() only rmtrees its own inner .overlay-probe
    subdir; the mkdtemp'd probe/home parents are cleaned up here. `home` is a
    separate empty dir (non-overlapping with the probe's scratch layers) so a bare
    host mounts it cleanly — the same real-home lowerdir shape the per-command
    mount uses."""
    if not sandbox_available() or not _OVERLAY_BASE.is_dir():
        return None
    try:
        probe = Path(tempfile.mkdtemp(prefix="imp-ovl-probe-", dir=_OVERLAY_BASE))
        home = Path(tempfile.mkdtemp(prefix="imp-ovl-home-", dir=_OVERLAY_BASE))
    except OSError:
        return None
    try:
        return overlay_mode(probe, home)
    finally:
        shutil.rmtree(probe, ignore_errors=True)
        shutil.rmtree(home, ignore_errors=True)


_OVERLAY_MODE: bool | None = _probe_overlay_at_base()
requires_overlay = pytest.mark.skipif(
    _OVERLAY_MODE is None,
    reason="unprivileged overlayfs probe at /var/tmp failed (read-only base or "
           "stacked overlay under a nested sandbox)")


requires_userxattr_overlay = pytest.mark.skipif(
    _OVERLAY_MODE is not True,
    reason="unprivileged overlayfs here does not accept -o userxattr (kernel < v5.11); "
           "the opaque-marker fix cannot apply")


def test_requires_overlay_skip_reason_is_verbatim() -> None:
    assert requires_overlay.kwargs["reason"] == (
        "unprivileged overlayfs probe at /var/tmp failed (read-only base or "
        "stacked overlay under a nested sandbox)")


def _make_overlay_root() -> Path:
    """A throwaway base dir OUTSIDE /tmp for the overlay tests (see _OVERLAY_BASE)."""
    return Path(tempfile.mkdtemp(prefix="imp-test-ovl-", dir=_OVERLAY_BASE))


def _overlay_home(base: Path, home: Path, *,
                  secret_dirs: tuple[str, ...] = (),
                  writable_caches: tuple[str, ...] = ()):
    """Active policy whose EVERY spawn mints its own COW-overlay layer under a
    per-step overlay root, exactly as a real spawn does (the bash tool calls
    prepare_spawn_policy). `home` and the sandbox worktree are SIBLINGS under
    `base` (a non-/tmp real fs), so no later bind_plan bind (/tmp identity,
    worktree rw) covers `home` and shadows the overlay merged mount. Returns
    (policy, cleanup); cleanup removes the whole overlay root."""
    wt = base / "worktree"
    wt.mkdir(exist_ok=True)
    base_pol = _active_policy(wt, home=home, secret_dirs=secret_dirs)
    layer, overlay_root, cleanup = make_home_layer(
        str(home), root=base / "ovl", userxattr=_OVERLAY_MODE)
    pol = dataclasses.replace(
        base_pol, home=str(home), writable_caches=writable_caches,
        home_layer=layer, overlay_root=overlay_root)
    return pol, cleanup


def _fallback_home(root: Path, home: Path, *,
                   secret_dirs: tuple[str, ...] = ()) -> SandboxPolicy:
    """Active policy with a rw-fallback HomeLayer (no overlay) over `home` — a
    plain --bind home home (writes persist to the REAL home). The fallback writes
    THROUGH to the real home by design, so a /tmp-based root is fine here (the /tmp
    shadow re-binds the same host home dir the fallback already targets)."""
    base = _active_policy(root, home=home, secret_dirs=secret_dirs)
    layer, overlay_root, _ = make_home_layer(
        str(home), root=root / "ovl", userxattr=None)
    return dataclasses.replace(base, home=str(home), home_layer=layer,
                               overlay_root=overlay_root)


# ---------------------------------------------------------------------------
# git helpers (copied from test_worktree.py — self-contained)
# ---------------------------------------------------------------------------

def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=False,
    )


def _init_repo(root: Path) -> None:
    """A git repo with one commit on branch `main` and `.imp/` gitignored."""
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "t@t")
    _git(root, "config", "user.name", "t")
    (root / ".gitignore").write_text(".imp/\n.imp-isolated/\n")
    (root / "README.md").write_text("seed\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "seed")


# ---------------------------------------------------------------------------
# B10.1  Write-confinement: inside worktree → succeeds
# ---------------------------------------------------------------------------

def test_write_inside_worktree_succeeds() -> None:
    """Writing a file inside the rw worktree bind must succeed (rc=0, content readable)."""
    root = _make_root()
    try:
        pol = _active_policy(root)
        ctx = _ctx(root, pol)
        result = _bash(ctx, f"echo hello > {root}/f.txt && cat {root}/f.txt")
        assert result["returncode"] == 0
        assert "hello" in result["stdout"]
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_syntax_error_reaches_the_agent_as_the_commands_own_failure() -> None:
    """AC 1/2 end-to-end under real bwrap: an unterminated backquote is an
    ordinary command failure, not a sandbox-setup failure."""
    root = _make_root()
    try:
        ctx = _ctx(root, _active_policy(root))
        result = _bash(ctx, 'echo "`')
        # no status/error also means the retry loop never ran (AC 2)
        assert "status" not in result
        assert "error" not in result
        assert result["returncode"] == 2
        assert str(result["stderr"]).strip() != ""
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_missing_bind_source_is_still_a_sandbox_setup_failure() -> None:
    """AC 4: a genuine bwrap setup failure (a bind source that does not exist)
    is still detected and still reported as a sandbox failure. extra_readable is
    not existence-pruned by prepare_spawn_policy, so bwrap aborts before exec."""
    root = _make_root()
    try:
        pol = dataclasses.replace(_active_policy(root),
                                  extra_readable=(str(root / "gone"),))
        ctx = _ctx(root, pol)
        result = _bash(ctx, "echo hi")
        assert result["status"] == "sandbox_setup_failed"
        assert "did NOT run" in str(result["error"])
        assert "hi" not in str(result["stdout"])       # the command never ran
    finally:
        shutil.rmtree(root, ignore_errors=True)


# ---------------------------------------------------------------------------
# B10.2  Write-confinement: outside worktree → fails
# ---------------------------------------------------------------------------

def test_write_outside_worktree_fails() -> None:
    """Writes to the RO host floor (e.g. /etc) must fail.

    The sandbox starts with --ro-bind / / (step 1), then layered rw binds.
    /etc is in the RO floor and must reject writes.
    """
    root = _make_root()
    try:
        pol = _active_policy(root)
        ctx = _ctx(root, pol)
        result = _bash(ctx, "echo x > /etc/imp-should-not-write 2>&1; echo rc=$?")
        # If rc=0, the write somehow succeeded — that would be a sandbox escape.
        assert "rc=0" not in result["stdout"], (
            "write to /etc succeeded inside sandbox — RO floor not enforced"
        )
    finally:
        shutil.rmtree(root, ignore_errors=True)


# ---------------------------------------------------------------------------
# B10.3  rm outside worktree → file survives on host
# ---------------------------------------------------------------------------

def test_rm_outside_fails() -> None:
    """rm of a file outside the rw worktree must not delete it on the host.

    With the real-/tmp identity bind, /tmp is entirely rw inside the sandbox.
    The victim is /etc/hosts (always present, in the RO floor, outside all rw
    binds). rm inside the sandbox must fail; the host file must still exist.
    """
    root = _make_root()
    try:
        pol = _active_policy(root)
        ctx = _ctx(root, pol)
        # /etc/hosts is in the RO floor (--ro-bind / /) and never rw-bound.
        result = _bash(ctx, "rm -f /etc/hosts 2>&1; echo rc=$?")
        # rm must fail (permission denied) — RO floor is enforced.
        assert "rc=0" not in result["stdout"], (
            "rm /etc/hosts succeeded inside sandbox — RO floor not enforced"
        )
        # Host /etc/hosts must still exist.
        assert Path("/etc/hosts").exists(), "/etc/hosts deleted by sandbox rm"
    finally:
        shutil.rmtree(root, ignore_errors=True)


# ---------------------------------------------------------------------------
# B10.4  Secret env dropped: AWS_SECRET_ACCESS_KEY not visible inside sandbox
# ---------------------------------------------------------------------------

def test_secret_env_dropped() -> None:
    """The sandbox env allowlist (spec B.4) only passes PATH/HOME/LANG/LC_*/engine-managed IMP_*.

    AWS_SECRET_ACCESS_KEY is not in the allowlist and must NOT be visible inside
    the sandbox even when set in the ambient environment. We use a hand-built
    policy whose child_env explicitly excludes AWS_* (matching what _build_child_env
    produces — the same allowlist resolve_policy bakes).
    """
    root = _make_root()
    old = os.environ.get("AWS_SECRET_ACCESS_KEY")
    os.environ["AWS_SECRET_ACCESS_KEY"] = "supersecret"
    try:
        # Hand-built policy: child_env does NOT include AWS_SECRET_ACCESS_KEY.
        # This directly tests the out-of-band child environment: the spawn env is
        # the whole of what the child gets, so an ambient var absent from it cannot
        # appear inside the sandbox.
        # (resolve_policy produces the same child_env via _build_child_env, which only
        # allows PATH/HOME/USER/LOGNAME/LANG/TERM/TZ/LC_*/engine-managed IMP_* + env_passthrough.)
        pol = _active_policy(root)
        ctx = _ctx(root, pol)
        result = _bash(ctx, "echo [$AWS_SECRET_ACCESS_KEY]")
        assert "supersecret" not in result["stdout"], (
            "AWS_SECRET_ACCESS_KEY leaked into sandbox — env allowlist not enforced"
        )
        assert result["returncode"] == 0, f"bash failed: {result}"
        # The variable is unset inside the sandbox; shell expands it to empty.
        assert "[]" in result["stdout"], (
            f"Expected '[]' (empty var) in stdout, got: {result['stdout']!r}"
        )
    finally:
        if old is None:
            os.environ.pop("AWS_SECRET_ACCESS_KEY", None)
        else:
            os.environ["AWS_SECRET_ACCESS_KEY"] = old
        shutil.rmtree(root, ignore_errors=True)


# ---------------------------------------------------------------------------
# B10.5  /tmp persists across separate bash calls (same tmp_host bound to /tmp)
# ---------------------------------------------------------------------------

def test_tmp_persists_across_calls() -> None:
    """/tmp inside the sandbox is the real host /tmp (identity bind); it persists
    between bash invocations on the same ctx/policy (same /tmp re-bound each call).
    """
    root = _make_root()
    try:
        pol = _active_policy(root)
        ctx = _ctx(root, pol)
        # First call: write a marker to /tmp
        r1 = _bash(ctx, "echo marker > /tmp/x && echo written")
        assert r1["returncode"] == 0
        assert "written" in r1["stdout"]
        # Second call: read it back
        r2 = _bash(ctx, "cat /tmp/x")
        assert r2["returncode"] == 0, f"cat /tmp/x failed: {r2}"
        assert "marker" in r2["stdout"], (
            f"/tmp/x not readable in second call — persistence broken: {r2['stdout']!r}"
        )
    finally:
        shutil.rmtree(root, ignore_errors=True)


# ---------------------------------------------------------------------------
# B10.6  secret_dirs masked with --tmpfs: content not visible inside sandbox
# ---------------------------------------------------------------------------

def test_secret_dir_masked() -> None:
    """A directory in policy.secret_dirs gets a --tmpfs mount (last, after all binds),
    making its host content invisible inside the sandbox (tmpfs is empty).
    """
    root = _make_root()
    try:
        home = root / "home"
        ssh_dir = home / ".ssh"
        ssh_dir.mkdir(parents=True)
        id_rsa = ssh_dir / "id_rsa"
        id_rsa.write_text("SUPER_SECRET_KEY\n")

        pol = _active_policy(root, home=home, secret_dirs=(str(ssh_dir),))
        ctx = _ctx(root, pol)
        result = _bash(ctx, f"cat {ssh_dir}/id_rsa 2>&1; echo rc=$?")
        assert "SUPER_SECRET_KEY" not in result["stdout"], (
            f"secret key visible inside sandbox — --tmpfs mask not applied: {result['stdout']!r}"
        )
    finally:
        shutil.rmtree(root, ignore_errors=True)


# ---------------------------------------------------------------------------
# B10.7  network=False → --unshare-net in argv (pure argv assertion)
# ---------------------------------------------------------------------------

def test_network_off_adds_unshare_net() -> None:
    """build_sandbox_spawn with network=False must include --unshare-net (spec B.2 step 15)."""
    pol = SandboxPolicy(
        active=True,
        worktree="/tmp/fake-wt",
        git_common_dir="/tmp/fake-wt/.git",
        git_ro_remask=(),
        writable_caches=(),
        extra_readable=(),
        extra_writable=(),
        secret_dirs=(),
        secret_files=(),
        child_env=(("PATH", "/usr/bin:/bin"),),
        network=False,  # <-- network isolation
        note="",
    )
    argv = build_sandbox_spawn("curl example.com", cwd="/tmp/fake-wt", policy=pol,
                               engine_env={}, environ={}).argv
    assert "--unshare-net" in argv, (
        f"--unshare-net missing from argv when network=False: {argv}"
    )


# ---------------------------------------------------------------------------
# B10.8  Gate in its cwd succeeds and write lands on host worktree (spec L3)
# ---------------------------------------------------------------------------

def test_gate_writing_in_its_cwd_succeeds() -> None:
    """A gate (run_script) writing a file inside the worktree must succeed and the
    file must appear on the host (rw bind persisted the write).
    """
    from imp.engine.gates import run_script
    from imp.engine.types import Pause, SourceLoc, Step

    root = _make_root("imp-test-gate-")
    try:
        pol = _active_policy(root)
        step = Step(
            name="g",
            phase="p",
            kind="script",
            rules=(),
            on_max_retries=Pause(),
            max_loops=1,
            source=SourceLoc(file=Path(__file__), line=1),
            command="echo ok > gate_out.txt && cat gate_out.txt",
            timeout_seconds=30,
        )
        result = run_script(step, cwd=root, policy=pol)
        assert result["exit"] == 0, f"gate exited {result['exit']}: {result}"
        assert "ok" in result["stdout"]
        # The write must have landed on the HOST (rw bind persisted it).
        assert (root / "gate_out.txt").exists(), (
            "gate_out.txt not found on host — rw bind did not persist write"
        )
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_gate_sees_post_rename_title_not_frozen_empty() -> None:
    """Regression: name-on-intent freezes the policy with IMP_QUEST_TITLE="" at run
    start; after rename_quest the gate's fresh env_extra carries the real title.
    Reproduces the dogfood failure where a `${IMP_QUEST_*:?not set by engine}`
    guard fired inside the sandbox's engine-supplied environment.
    """
    import dataclasses

    from imp.engine.gates import run_script
    from imp.engine.types import Pause, SourceLoc, Step

    root = _make_root("imp-test-title-")
    try:
        pol = _active_policy(root)
        pol = dataclasses.replace(pol, child_env=(*pol.child_env, ("IMP_QUEST_TITLE", "")))
        step = Step(
            name="g", phase="p", kind="script", rules=(), on_max_retries=Pause(),
            max_loops=1, source=SourceLoc(file=Path(__file__), line=1),
            command=': "${IMP_QUEST_TITLE:?IMP_QUEST_TITLE not set by engine}"; '
                    'printf %s "$IMP_QUEST_TITLE"',
            timeout_seconds=30,
        )
        result = run_script(step, cwd=root, policy=pol,
                            env_extra={"IMP_QUEST_TITLE": "Live mode log polish"})
        assert result["exit"] == 0, f"gate exited {result['exit']}: {result}"
        assert result["stdout"] == "Live mode log polish"
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_bash_tool_uses_live_quest_title_not_stale_snapshot() -> None:
    """Regression: the bash tool must read IMP_QUEST_TITLE from the LIVE Quest, not
    the frozen quest_context snapshot that rename_quest never updates. Without the
    fix the agent's own bash sees the empty name-on-intent title inside the sandbox.
    """
    import types as _t

    root = _make_root("imp-test-bash-title-")
    try:
        pol = _active_policy(root)
        ctx = _ctx(root, pol)
        # quest_context = the pre-rename snapshot (empty title); ctx.quest = the
        # live, renamed Quest. The bash tool must prefer the live one.
        ctx.quest_context = QuestContext(
            id="2026-06-10-x", title="",
            project_root=root, workflow_dir=root / ".imp" / "wf")
        ctx.quest = _t.SimpleNamespace(
            id="2026-06-10-x", handle="", title="renamed")
        out = _bash(ctx, 'printf %s "$IMP_QUEST_TITLE"')
        assert out["returncode"] == 0, out
        assert out["stdout"] == "renamed"
    finally:
        shutil.rmtree(root, ignore_errors=True)


# ---------------------------------------------------------------------------
# B10.9  SECURITY: git commit works but ALL escape vectors blocked (spec M1/F-9)
# ---------------------------------------------------------------------------

def test_git_commit_works_but_escape_vectors_blocked() -> None:
    """Security-critical test (spec M1/F-9, B10 Step 2).

    Proves the sandbox enforces the git remask policy:
      - git commit INSIDE the sandbox succeeds and persists to the host (.git rw bind).
      - Writing .git/hooks/post-commit is BLOCKED (.git/hooks is remasked RO).
      - git config core.hooksPath /tmp/evil is BLOCKED (.git/config is remasked RO).
      - git config --worktree core.hooksPath /tmp/evil is BLOCKED (.git/config remasked RO).
      - Writing a SIBLING worktree's config.worktree is BLOCKED (the whole
        .git/worktrees tree is masked; only self is re-exposed).
      - Writing .git/info/attributes succeeds but the filter DRIVER config write is BLOCKED.
    """
    control = Path(tempfile.mkdtemp(prefix="imp-test-gitesc-"))
    try:
        # --- repo setup ---
        _init_repo(control)
        # Create a sibling linked worktree BEFORE resolving policy. Its admin dir
        # (and any config.worktree under it) is hidden by the .git/worktrees tmpfs
        # mask; only self (esc1) is re-exposed.
        wt.create_worktree(control, "sib", "main")
        # Create the sibling's config.worktree explicitly so vector 5 has a concrete
        # sibling path to attempt a write against (the mask makes it unreachable).
        sib_config_wt = control / ".git" / "worktrees" / "sib" / "config.worktree"
        sib_config_wt.write_text("[core]\n\textensions = worktreeConfig\n")
        wt_path = wt.create_worktree(control, "esc1", "main")
        # control/.imp/tmp/esc1 is created by create_worktree (B4).

        quest_dir = project_state_dir(control) / "quests" / "esc1"
        quest_dir.mkdir(parents=True)
        pol = resolve_policy(
            worktree=wt_path,
            control_root=control,
            quest_id="esc1",
            cfg=SandboxConfig(
                home=control / "home",
                uid=os.getuid(),
            ),
            engine_env_result={"IMP_PROJECT_ROOT": str(wt_path)},
            environ=os.environ,
            available=True,
            quest_dir=quest_dir,
        )
        ctx = ToolContext(
            project_root=wt_path,
            events_path=wt_path / "events.jsonl",
        )
        ctx.quest_context = QuestContext(
            title="Escape Test",
            project_root=wt_path,
            workflow_dir=wt_path / ".imp" / "wf",
        )
        ctx.base_sandbox_policy = pol
        own_tool_context(ctx)

        # ------------------------------------------------------------------
        # 1. git commit SUCCEEDS and commit is visible on the host
        #    The worktree and .git (git_common_dir) are both rw-bound, so git
        #    writes to objects/refs/index/logs persist to the host filesystem.
        #    We override user.email/user.name inline (-c) since .git/config is
        #    remasked RO and may not have identity configured.
        # ------------------------------------------------------------------
        r1 = _bash(
            ctx,
            "echo change > tracked.txt && git add tracked.txt && "
            "git -c user.email=t@t -c user.name=t commit -qm sandboxed && "
            "echo COMMIT_OK",
        )
        assert "COMMIT_OK" in r1["stdout"], (
            f"git commit inside sandbox failed — rw bind not working: {r1}"
        )
        # Verify on the HOST that the "sandboxed" commit actually landed.
        log = _git(wt_path, "log", "--oneline")
        assert "sandboxed" in log.stdout, (
            f"'sandboxed' commit not visible in host git log: {log.stdout!r}"
        )

        # ------------------------------------------------------------------
        # 2. Writing .git/hooks/post-commit FAILS
        #    .git/hooks is in git_ro_remask → --ro-bind over the rw .git bind;
        #    bwrap last-wins so hooks/ is RO inside the namespace.
        # ------------------------------------------------------------------
        hooks_post = control / ".git" / "hooks" / "post-commit"
        r2 = _bash(
            ctx,
            f"echo '#!/bin/sh' > {hooks_post} 2>&1; echo rc=$?",
        )
        assert "rc=0" not in r2["stdout"], (
            f"Write to .git/hooks/post-commit succeeded — hooks remask NOT enforced: {r2}"
        )

        # ------------------------------------------------------------------
        # 3. git config core.hooksPath /tmp/evil FAILS
        #    .git/config is in git_ro_remask → RO inside the namespace.
        #    git config needs to write .git/config → must be denied.
        # ------------------------------------------------------------------
        r3 = _bash(ctx, "git config core.hooksPath /tmp/evil 2>&1; echo rc=$?")
        assert "rc=0" not in r3["stdout"], (
            f"git config core.hooksPath succeeded — .git/config remask NOT enforced: {r3}"
        )

        # ------------------------------------------------------------------
        # 4. git config --worktree core.hooksPath /tmp/evil FAILS
        #    The escape is blocked transitively: `git config --worktree` requires
        #    extensions.worktreeConfig in .git/config first, and .git/config is
        #    remasked RO — so git refuses (rc=128, "worktreeConfig not enabled")
        #    and never reaches a writable hooksPath. self's own config.worktree is
        #    now writable (under the self re-expose) but this ro-mask still blocks it;
        #    sibling config.worktree files are unreachable behind the tmpfs (vector 5).
        # ------------------------------------------------------------------
        r4 = _bash(
            ctx,
            "git config --worktree core.hooksPath /tmp/evil 2>&1; echo rc=$?",
        )
        assert "rc=0" not in r4["stdout"], (
            f"git config --worktree hooksPath succeeded — config.worktree remask NOT enforced: {r4}"
        )

        # ------------------------------------------------------------------
        # 5. Writing a SIBLING worktrees/sib/config.worktree FAILS
        #    .git/worktrees is masked by a tmpfs and only self (esc1) is
        #    re-exposed, so the sibling path is not present inside the sandbox.
        # ------------------------------------------------------------------
        sib_config = control / ".git" / "worktrees" / "sib" / "config.worktree"
        assert sib_config.exists(), (
            f"sibling config.worktree not found at {sib_config} — test setup error"
        )
        r5 = _bash(
            ctx,
            f"echo x > {sib_config} 2>&1; echo rc=$?",
        )
        assert "rc=0" not in r5["stdout"], (
            f"Write to sibling config.worktree succeeded — sibling remask NOT enforced: {r5}"
        )

        # ------------------------------------------------------------------
        # 6. Writing .git/info/attributes SUCCEEDS (info/ is under rw .git,
        #    not remasked) BUT git config filter.evil.clean /tmp/evil.sh FAILS
        #    (.git/config is RO → driver registration blocked).
        #    The protection: attributes alone are INERT without a config-defined
        #    filter driver; the driver can only be added via .git/config (RO).
        # ------------------------------------------------------------------
        r6a = _bash(
            ctx,
            f"echo '* filter=evil' > {control}/.git/info/attributes 2>&1; echo rc=$?",
        )
        # The attributes write may succeed (info/ is rw) — that's expected.
        # The key invariant is that the filter driver can't be registered.
        r6b = _bash(
            ctx,
            "git config filter.evil.clean /tmp/evil.sh 2>&1; echo rc=$?",
        )
        assert "rc=0" not in r6b["stdout"], (
            f"git config filter.evil.clean succeeded — .git/config remask NOT enforced "
            f"(attributes write r6a rc: {r6a['stdout']!r}): {r6b}"
        )

    finally:
        # Clean up worktrees before rmtree (git bookkeeping).
        with contextlib.suppress(Exception):
            wt.remove_worktree(control, "esc1", force=True)
        with contextlib.suppress(Exception):
            wt.remove_worktree(control, "sib", force=True)
        shutil.rmtree(control, ignore_errors=True)


# ---------------------------------------------------------------------------
# PYTHONPATH passthrough: sandboxed `import imp` resolves to the worktree's src
# ---------------------------------------------------------------------------

def test_sandboxed_python_imports_worktree_imp() -> None:
    """With PYTHONPATH carried in the live engine_env, a sandboxed
    `python3 -c "import imp; print(imp.__file__)"` resolves imp under the
    worktree's own src — proving the import shadow survives the env allowlist."""
    root = _make_root()
    try:
        worktree_imp = root / "src" / "imp"
        worktree_imp.mkdir(parents=True)
        (worktree_imp / "__init__.py").write_text("")
        pol = _active_policy(root)
        spawn = build_sandbox_spawn(
            "python3 -c 'import imp; print(imp.__file__)'",
            cwd=str(root), policy=pol,
            engine_env={"PYTHONPATH": str(root / "src")}, environ={})
        result = subprocess.run(spawn.argv, env=spawn.env,
                                capture_output=True, text=True)
        assert result.returncode == 0, result.stderr
        assert str(worktree_imp) in result.stdout
    finally:
        shutil.rmtree(root, ignore_errors=True)


# ---------------------------------------------------------------------------
# PATH resolution: inside a sandboxed step a bare `imp` runs the OUTER (running)
# imp, NOT the worktree's shadowed code. src/imp/engine/shim.py writes a per-run
# shim that sandbox._build_child_env prepends to PATH; the shim puts the outer src
# root at sys.path[0], so it beats the injected worktree PYTHONPATH shadow.
# ---------------------------------------------------------------------------

def test_sandboxed_imp_resolves_outer_imp_over_worktree_shadow() -> None:
    """Under a real bwrap policy carrying both the shim dir on PATH and a
    worktree PYTHONPATH shadow, `imp` runs the outer CLI while a plain
    `import imp` still resolves to the shadow."""
    sentinel = "WORKTREE-IMP-SENTINEL-voli59"
    root = _make_root()
    try:
        pkg = root / "src" / "imp"   # sentinel package: deliberately NO `cli` subpackage
        pkg.mkdir(parents=True)
        (pkg / "__init__.py").write_text(f'SENTINEL = "{sentinel}"\n')
        (pkg / "__main__.py").write_text(
            f'def main():\n    print("{sentinel}")\n    return 0\n')

        shim_dir = write_imp_shim(root / "qd")
        base = _active_policy(root)
        # PATH exactly as _build_child_env composes it: shim dir first, ambient behind.
        path = os.pathsep.join(
            [str(shim_dir), *(v for k, v in base.child_env if k == "PATH")])
        pol = dataclasses.replace(
            base,
            child_env=tuple((k, path if k == "PATH" else v) for k, v in base.child_env))
        engine_env = {"PYTHONPATH": str(root / "src")}

        def _run(cmd: str) -> subprocess.CompletedProcess[str]:
            spawn = build_sandbox_spawn(cmd, cwd=str(root), policy=pol,
                                        engine_env=engine_env, environ={})
            return subprocess.run(spawn.argv, env=spawn.env,
                                  capture_output=True, text=True)

        version = _run("imp --version")
        assert version.returncode == 0, (
            f"sandboxed `imp --version` failed — the shim did not beat the "
            f"PYTHONPATH shadow: stdout={version.stdout!r} stderr={version.stderr!r}")
        assert version.stdout.strip() == f"imp {__version__}", (
            f"sandboxed `imp` did not run the OUTER imp: "
            f"stdout={version.stdout!r} stderr={version.stderr!r}")
        assert sentinel not in version.stdout and sentinel not in version.stderr, (
            f"worktree sentinel leaked into the outer imp's output: "
            f"stdout={version.stdout!r} stderr={version.stderr!r}")

        shadow = _run("python3 -c 'import imp; print(imp.__file__)'")
        assert shadow.returncode == 0, shadow.stderr
        assert shadow.stdout.strip().startswith(str(pkg) + os.sep), (
            f"non-vacuity control failed: the PYTHONPATH shadow was NOT armed in "
            f"this sandbox, so the `imp --version` assertion proves nothing: "
            f"stdout={shadow.stdout!r} stderr={shadow.stderr!r}")

        which = _run("command -v imp")
        assert which.returncode == 0, which.stderr
        assert which.stdout.strip() == str(shim_dir / "imp"), (
            f"`imp` on PATH is not the test's shim: stdout={which.stdout!r}")
    finally:
        shutil.rmtree(root, ignore_errors=True)


# ---------------------------------------------------------------------------
# Git-ref protection (nate82): real-bwrap integration + regression
# ---------------------------------------------------------------------------

def _refprot_repo(prefix: str) -> tuple[Path, Path]:
    """A real control repo (branch `main`, one seed commit) plus a linked change
    worktree on branch `imp/esc1` — the non-draft layout the guard protects."""
    control = Path(tempfile.mkdtemp(prefix=prefix))
    _init_repo(control)
    wt_path = wt.create_worktree(control, "esc1", "main")
    return control, wt_path


def _refprot_ctx(control: Path, worktree: Path) -> tuple[ToolContext, SandboxPolicy]:
    """Resolve the REAL policy for `worktree` under `control` and wrap it in a
    ToolContext the `bash` tool can drive. worktree != control → the git-ref
    guard is active (Part 1); a materialized quest_dir makes the .git/worktrees
    self re-expose emit so the linked worktree's own admin dir stays writable.
    Used for the change worktree and, with a different `worktree`, for the spike
    worktree."""
    quest_dir = project_state_dir(control) / "quests" / "esc1"
    quest_dir.mkdir(parents=True, exist_ok=True)
    pol = resolve_policy(
        worktree=worktree, control_root=control, quest_id="esc1",
        cfg=SandboxConfig(home=control / "home", uid=os.getuid()),
        engine_env_result={"IMP_PROJECT_ROOT": str(worktree)},
        environ=os.environ, available=True, quest_dir=quest_dir,
    )
    ctx = ToolContext(
        project_root=worktree, events_path=worktree / "events.jsonl")
    ctx.quest_context = QuestContext(
        title="Ref Test", project_root=worktree,
        workflow_dir=worktree / ".imp" / "wf")
    ctx.base_sandbox_policy = pol
    return own_tool_context(ctx), pol


def _seed_stash(control: Path) -> str:
    """Create a real refs/stash on the host so the guard's EXISTENCE-GATED mask
    (resolve_policy → _existing) actually covers it — a fresh repo has no
    refs/stash, so without this the stash mask is silently dropped and stays
    writable. Leaves the working tree clean (the dirt is stashed away). Returns
    the stash commit sha for an unchanged-after assertion."""
    (control / "README.md").write_text("stash seed\n")
    _git(control, "stash", "push", "-m", "seed-stash")
    sha = _git(control, "rev-parse", "refs/stash").stdout.strip()
    assert sha, "seed stash was not created — refs/stash mask would be untested"
    return sha


def test_control_root_shared_ref_mutation_blocked() -> None:
    """Inside the sandbox, mutations of the control root's shared refs all FAIL:
    update-ref on the base branch (refs/heads RO), git stash (refs/stash + index
    RO), and a bare commit -a on the base branch (index RO). The host base ref
    and the pre-existing stash ref are proven unchanged.

    A refs/stash is pre-seeded on the host BEFORE the policy is resolved: the mask
    is existence-gated (resolve_policy → _existing), so without a pre-existing
    refs/stash the stash mask is dropped and a first stash would not be blocked.

    Falsifiability: without the Part-1 masks, .git is bound fully rw, so all three
    commands would succeed (rc=0), refs/heads/main would move to `other_sha`, and
    the in-sandbox stash would push a NEW refs/stash tip (!= stash_before).
    """
    control, wt_path = _refprot_repo("imp-test-refblock-")
    try:
        # Give imp/esc1 a distinct commit so a successful update-ref of main to it
        # would be an observable MOVE (this runs on the host — no mask there).
        (wt_path / "w.txt").write_text("w\n")
        _git(wt_path, "add", "-A")
        _git(wt_path, "-c", "user.email=t@t", "-c", "user.name=t",
             "commit", "-qm", "branch work")
        other_sha = _git(wt_path, "rev-parse", "HEAD").stdout.strip()
        main_before = _git(control, "rev-parse", "refs/heads/main").stdout.strip()
        assert other_sha != main_before
        # Pre-create refs/stash on the host so the existence-gated mask covers it.
        stash_before = _seed_stash(control)
        # Re-dirty the control tree (the seed dirt was stashed away) so the
        # in-sandbox `stash` / `commit -a` have real work to do — a clean tree
        # would no-op with rc=0 and never touch the masked paths.
        (control / "README.md").write_text("dirty\n")

        ctx, _pol = _refprot_ctx(control, wt_path)

        r1 = _bash(
            ctx,
            f"git -C {control} update-ref refs/heads/main {other_sha} "
            "2>&1; echo rc=$?")
        assert "rc=0" not in r1["stdout"], (
            f"update-ref on base branch succeeded — refs/heads mask NOT enforced: {r1}")

        r2 = _bash(ctx, f"git -C {control} stash 2>&1; echo rc=$?")
        assert "rc=0" not in r2["stdout"], (
            f"git stash succeeded — refs/stash / index mask NOT enforced: {r2}")
        # `git stash` computes the stash commit (objects rw) but cannot update the
        # masked refs/stash file nor the masked index → rc!=0 and the ref does not
        # advance past the seed stash.

        # Re-dirty the tree so the bare-commit probe exercises the index mask
        # INDEPENDENTLY: in a counterfactual no-guard run the stash above would
        # succeed and reset the working tree, leaving `commit -am` with nothing to
        # stage (a spurious rc=1). Re-dirtying keeps r3 an unambiguous index-mask
        # probe in every run.
        (control / "README.md").write_text("dirty again\n")
        r3 = _bash(
            ctx,
            f"git -C {control} -c user.email=t@t -c user.name=t "
            "commit -am pwned 2>&1; echo rc=$?")
        assert "rc=0" not in r3["stdout"], (
            f"bare commit on base branch succeeded — index mask NOT enforced: {r3}")

        # HOST invariants: the base ref did not move; the stash ref did not advance
        # past the pre-seeded tip (no new stash was pushed).
        assert _git(
            control, "rev-parse", "refs/heads/main").stdout.strip() == main_before, (
            "refs/heads/main moved despite the mask")
        assert _git(
            control, "rev-parse", "refs/stash").stdout.strip() == stash_before, (
            "refs/stash advanced despite the mask")
    finally:
        with contextlib.suppress(Exception):
            wt.remove_worktree(control, "esc1", force=True)
        shutil.rmtree(control, ignore_errors=True)


def test_denied_ref_write_carries_sandbox_note() -> None:
    """A real masked-ref refusal explains itself: nonzero exit, the raw git error
    on stderr, and the sandbox_note beside it.

    stderr is deliberately NOT redirected into stdout here (unlike the sibling
    ref-mutation tests): the note is derived from stderr, so a `2>&1` would test
    nothing. git's message under the mask is `cannot lock ref … Read-only file
    system`, which is exactly what the marker set keys on.

    Falsifiability: with the masks removed the write would succeed (rc 0) and the
    note would be absent; with the note keyed on the wrong stream or on
    proc.returncode the field would be missing and the membership assertion would
    fail.
    """
    control, wt_path = _refprot_repo("imp-test-refnote-")
    try:
        head = _git(control, "rev-parse", "HEAD").stdout.strip()
        ctx, _pol = _refprot_ctx(control, wt_path)

        r = _bash(ctx, f"git -C {control} update-ref refs/heads/probe {head}")

        assert r["returncode"] != 0, (
            f"masked ref write succeeded — refs/heads mask NOT enforced: {r}")
        assert "sandbox_note" in r, (
            f"denied ref write carried no sandbox_note: {r}")
        assert str(r["sandbox_note"]).startswith(
            "a write was refused by imp's sandbox"), r
        assert "Read-only file system" in str(r["stderr"]), (
            f"raw OS error was swallowed instead of supplemented: {r}")
        assert not (control / ".git" / "refs" / "heads" / "probe").exists(), (
            "the probe ref was actually created on the host")
    finally:
        with contextlib.suppress(Exception):
            wt.remove_worktree(control, "esc1", force=True)
        shutil.rmtree(control, ignore_errors=True)


def test_change_and_spike_branch_writes_succeed() -> None:
    """The finalize-flow guard: commits + autosquash rebase on the change branch
    imp/esc1 succeed, and a commit on a spike branch imp/spike-esc1-1 succeeds —
    both branches live under the rw re-exposed refs/heads/imp.

    Falsifiability: if the refs/heads RO mask were NOT re-exposed for the imp/
    subtree, every commit/rebase here would fail (rc!=0) and the CHANGE_OK /
    SPIKE_OK markers would be absent; the host commit-count / spike-ref assertions
    would then fail.
    """
    control, wt_path = _refprot_repo("imp-test-refok-")
    try:
        ctx, _pol = _refprot_ctx(control, wt_path)

        # Quest branch: two commits then an autosquash rebase onto main. The
        # inline GIT_SEQUENCE_EDITOR/GIT_EDITOR=: keep rebase -i non-interactive
        # (they are shell-assignment prefixes, independent of the spawn environment).
        r1 = _bash(
            ctx,
            "echo a > f.txt && git add f.txt && "
            "git -c user.email=t@t -c user.name=t commit -qm work && "
            "echo b >> f.txt && git add f.txt && "
            "git -c user.email=t@t -c user.name=t commit -qm 'fixup! work' && "
            "GIT_SEQUENCE_EDITOR=: GIT_EDITOR=: "
            "git -c user.email=t@t -c user.name=t rebase -i --autosquash main && "
            "echo CHANGE_OK")
        assert "CHANGE_OK" in r1["stdout"], (
            f"change-branch commit/rebase failed — refs/heads/imp re-expose broken: {r1}")
        # Host: imp/esc1 advanced and the fixup collapsed it to a single commit
        # over main (proves the writes + rebase actually landed).
        assert _git(
            control, "rev-list", "--count",
            "main..imp/esc1").stdout.strip() == "1", "imp/esc1 did not collapse to 1 commit"

        # Spike branch: a fresh spike worktree on imp/spike-esc1-1, policy rooted
        # at the spike worktree (mirrors _build_isolated_policy inheriting the guard).
        spike_wt = wt.create_spike_worktree(control, "esc1", 1, "main")
        spike_ctx, _ = _refprot_ctx(control, spike_wt)
        r2 = _bash(
            spike_ctx,
            "echo s > s.txt && git add s.txt && "
            "git -c user.email=t@t -c user.name=t commit -qm spike && echo SPIKE_OK")
        assert "SPIKE_OK" in r2["stdout"], (
            f"spike-branch commit failed — refs/heads/imp re-expose broken: {r2}")
        assert _git(
            control, "rev-parse", "--verify", "--quiet",
            "refs/heads/imp/spike-esc1-1").returncode == 0, "spike branch ref not updated"
    finally:
        with contextlib.suppress(Exception):
            wt.destroy_spike_worktree(control, "esc1", 1)
        with contextlib.suppress(Exception):
            wt.remove_worktree(control, "esc1", force=True)
        shutil.rmtree(control, ignore_errors=True)


def test_imp_own_git_writes_unaffected_by_ref_masks() -> None:
    """Regression: the guard must not touch imp's own in-sandbox .git writes.
    refs/imp/* and .git/imp/ are writable inside a real sandbox, and the real
    artifact_commit lands refs/imp/artifacts/<id>. The policy mirror agrees each
    artifact_commit path is writable while the masked crown jewels deny.

    Falsifiability: if a mask accidentally covered refs/imp or .git/imp, the
    in-sandbox probes would fail (rc!=0) and the host ref/file would be absent;
    the allows_write assertions on refs/imp/artifacts and .git/imp/repo.lock
    would flip to False.
    """
    control, wt_path = _refprot_repo("imp-test-artifact-")
    try:
        # Pre-create refs/stash so the existence-gated stash mask is actually
        # applied (a fresh repo has none → it would be dropped and stay writable,
        # making the allows_write(refs/stash) assertion below read True).
        _seed_stash(control)
        ctx, pol = _refprot_ctx(control, wt_path)
        head = _git(control, "rev-parse", "HEAD").stdout.strip()

        # refs/imp/* is NOT masked → update-ref succeeds in-sandbox and lands on host.
        r1 = _bash(
            ctx,
            f"git -C {control} update-ref refs/imp/probe {head} 2>&1; echo rc=$?")
        assert "rc=0" in r1["stdout"], (
            f"update-ref refs/imp/probe failed — refs/imp wrongly masked: {r1}")
        assert _git(
            control, "rev-parse", "--verify", "--quiet",
            "refs/imp/probe").stdout.strip() == head, "refs/imp/probe not written on host"

        # .git/imp scratch (RepoGitLock + artifact_commit's temp index) is writable.
        r2 = _bash(
            ctx,
            f"mkdir -p {control}/.git/imp && "
            f"echo x > {control}/.git/imp/probe.lock 2>&1; echo rc=$?")
        assert "rc=0" in r2["stdout"], (
            f"write under .git/imp failed — .git/imp wrongly masked: {r2}")
        assert (control / ".git" / "imp" / "probe.lock").exists(), (
            ".git/imp/probe.lock not written on host")

        # The real artifact_commit writes refs/imp/artifacts/esc1 (full-tree snapshot).
        art = wt_path / "artifact.md"
        art.write_text("hello\n")
        wrote = wt.artifact_commit(
            control, "esc1", {"spec/esc1.md": art}, message="snap")
        assert wrote is True
        assert _git(
            control, "rev-parse", "--verify", "--quiet",
            "refs/imp/artifacts/esc1").returncode == 0, "artifact ref not created"

        # Policy mirror: every artifact_commit write path is writable; the three
        # masked crown jewels deny.
        git = control / ".git"
        assert pol.allows_write(git / "refs" / "imp" / "artifacts" / "esc1") is True
        assert pol.allows_write(git / "imp" / "repo.lock") is True
        assert pol.allows_write(git / "objects" / "ab" / "cdef") is True
        assert pol.allows_write(git / "refs" / "heads" / "main") is False
        assert pol.allows_write(git / "refs" / "stash") is False
        assert pol.allows_write(git / "index") is False
    finally:
        with contextlib.suppress(Exception):
            wt.remove_worktree(control, "esc1", force=True)
        shutil.rmtree(control, ignore_errors=True)


def test_vanished_ref_reexpose_dir_recreated_at_spawn() -> None:
    """.git/refs/heads/imp deleted between policy bake and spawn must not wedge the
    sandbox: prepare_spawn_policy re-creates it before bwrap binds it, and the
    command runs normally.

    Falsifiability: without the spawn-time mkdir, bwrap dies in setup on the missing
    bind source for all 5 attempts, so the tool returns status=sandbox_setup_failed
    with no REFDIR_OK in stdout and the directory is still absent on the host.
    """
    control, wt_path = _refprot_repo("imp-test-refdir-")
    try:
        ctx, pol = _refprot_ctx(control, wt_path)
        imp_refs = control / ".git" / "refs" / "heads" / "imp"
        assert str(imp_refs) in pol.git_ref_reexpose
        shutil.rmtree(imp_refs)

        r = _bash(ctx, "echo REFDIR_OK")

        assert r.get("status") != "sandbox_setup_failed", r
        assert "REFDIR_OK" in r["stdout"], r
        assert imp_refs.is_dir(), "the re-exposed ref dir was not re-created at spawn"
    finally:
        with contextlib.suppress(Exception):
            wt.remove_worktree(control, "esc1", force=True)
        shutil.rmtree(control, ignore_errors=True)


def test_pack_refs_pruning_ref_dir_leaves_spawn_and_quest_commit_working() -> None:
    """The production trigger at the ref level: `git pack-refs --all` packs
    refs/heads/imp/* into packed-refs and prunes the emptied refs/heads/imp
    directory. The next spawn must still enter the sandbox AND still write the quest
    branch's loose ref — the reason the directory is re-created rather than dropped
    from the bind plan.

    Falsifiability: without the spawn-time mkdir the tool returns
    status=sandbox_setup_failed and imp/esc1 never advances; were the bind dropped
    instead of re-created, refs/heads would stay read-only and the commit would fail
    with no PACKED_OK.
    """
    control, wt_path = _refprot_repo("imp-test-packrefs-")
    try:
        ctx, _pol = _refprot_ctx(control, wt_path)
        imp_refs = control / ".git" / "refs" / "heads" / "imp"
        before = _git(control, "rev-parse", "refs/heads/imp/esc1").stdout.strip()
        assert before, "quest branch ref missing — the trigger would be untested"

        _git(control, "pack-refs", "--all")
        assert not imp_refs.exists(), "pack-refs did not prune the empty ref dir"

        r = _bash(
            ctx,
            "echo p > p.txt && git add p.txt && "
            "git -c user.email=t@t -c user.name=t commit -qm packed && echo PACKED_OK")

        assert r.get("status") != "sandbox_setup_failed", r
        assert "PACKED_OK" in r["stdout"], r
        after = _git(control, "rev-parse", "refs/heads/imp/esc1").stdout.strip()
        assert after != before, "quest branch did not advance after the packed spawn"
        assert (imp_refs / "esc1").is_file(), (
            "loose quest ref not written — refs/heads/imp is not writable")
    finally:
        with contextlib.suppress(Exception):
            wt.remove_worktree(control, "esc1", force=True)
        shutil.rmtree(control, ignore_errors=True)


# ---------------------------------------------------------------------------
# Overlay-home behavioral tests (AC 1-5)
# ---------------------------------------------------------------------------

@requires_overlay
def test_overlay_home_faithful_isolated_and_masked() -> None:
    # AC 1: a real home file (.gitconfig) reads through the overlay lower; a new
    #       $HOME write is readable in-step but leaves the real home unchanged
    #       (captured in the discarded upper).
    # AC 5: ~/.claude is readable (model auth intact).
    # AC 3 (overlay mode): a secret dir (.ssh) is masked to empty — proving the
    #       overlay home bind is emitted BEFORE the secret masks.
    # home + worktree are siblings under a non-/tmp base so the overlay is not
    # shadowed by the /tmp/worktree binds (see _overlay_home).
    root = _make_overlay_root()
    try:
        home = root / "home"
        home.mkdir()
        (home / ".gitconfig").write_text("[user]\n\tname = Real Dev\n")
        (home / ".claude").mkdir()
        (home / ".claude" / "creds.json").write_text("MODEL_TOKEN_XYZ\n")
        ssh = home / ".ssh"
        ssh.mkdir()
        (ssh / "id_rsa").write_text("SUPER_SECRET_KEY\n")

        pol, cleanup = _overlay_home(root, home, secret_dirs=(str(ssh),))
        try:
            ctx = _ctx(Path(pol.worktree), pol)
            r = _bash(
                ctx,
                'cat "$HOME/.gitconfig"; echo "==C=="; '
                'cat "$HOME/.claude/creds.json"; echo "==S=="; '
                'cat "$HOME/.ssh/id_rsa" 2>&1; echo "==W=="; '
                'echo NEWCONTENT > "$HOME/scratch.txt" && cat "$HOME/scratch.txt"')
            out = r["stdout"]
            assert "Real Dev" in out                 # AC1: real home read-through
            assert "MODEL_TOKEN_XYZ" in out          # AC5: .claude readable
            assert "SUPER_SECRET_KEY" not in out     # AC3: .ssh masked in overlay
            assert "NEWCONTENT" in out               # AC1: write+readback in step
            # AC1: the write was captured in the discarded upper — real home untouched.
            assert not (home / "scratch.txt").exists()
        finally:
            cleanup()
    finally:
        shutil.rmtree(root, ignore_errors=True)


@requires_overlay
def test_overlay_home_write_persists_within_a_run_and_not_across() -> None:
    # AC 13, the accepted semantics change of §5: the layer is minted once per
    # NAMESPACE, so a $HOME write in one bash call is readable in the next of the
    # same agent run. A fresh run gets a fresh layer and starts clean, and the
    # user's real home (the lowerdir) never sees the file either way.
    root = _make_overlay_root()
    try:
        home = root / "home"
        home.mkdir()
        pol, cleanup = _overlay_home(root, home)
        try:
            ctx = _ctx(Path(pol.worktree), pol)
            r1 = _bash(
                ctx, 'echo PERSISTED > "$HOME/across.txt" && cat "$HOME/across.txt"')
            assert r1["returncode"] == 0, f"first call failed: {r1}"
            assert "PERSISTED" in r1["stdout"]
            r2 = _bash(ctx, 'cat "$HOME/across.txt" || echo MISSING')
            assert r2["returncode"] == 0, f"second call did not run: {r2}"
            assert "PERSISTED" in r2["stdout"], (
                f"the write did not carry across the call: {r2['stdout']!r}")
            # falsifier: the isolated real home (the lowerdir) never saw the file.
            assert not (home / "across.txt").exists()
            # and a FRESH agent run starts clean — its own context, its own layer.
            fresh = _ctx(Path(pol.worktree), pol)
            r3 = _bash(fresh, 'cat "$HOME/across.txt" || echo MISSING')
            assert "MISSING" in r3["stdout"], (
                f"the write leaked into a new agent run: {r3['stdout']!r}")
        finally:
            cleanup()
    finally:
        shutil.rmtree(root, ignore_errors=True)


@requires_overlay
def test_concurrent_background_and_foreground_share_one_layer() -> None:
    # Two sandboxed commands CONCURRENTLY LIVE within one agent run — a background
    # command still running while a foreground command runs — used to be the
    # ovl_inuse hazard, because each spawn minted its own layer over one root. The
    # keeper mints ONE layer and mounts it once, so the hazard is gone by
    # construction and the two commands share a $HOME, exactly as they share a
    # process table. The handshake through the shared rw worktree makes the
    # overlap deterministic rather than timing-dependent: the background command
    # publishes `bg-ready` and then blocks until `release`, which the foreground
    # command creates only AFTER writing and listing its own $HOME.
    root = _make_overlay_root()
    try:
        home = root / "home"
        home.mkdir()
        pol, cleanup = _overlay_home(root, home)
        try:
            ctx = _ctx(Path(pol.worktree), pol)
            ctx.background_enabled = True
            ctx.agent_id = "step:build/coder"
            # The loop bounds are hang-detectors, not tuned windows: they never
            # false-fire under load, and the outer pytest timeout catches a
            # genuine hang first.
            bg_cmd = (
                'echo BG > "$HOME/bg.txt"; : > bg-ready; i=0; '
                'while [ ! -e release ] && [ $i -lt 3000 ]; '
                'do sleep 0.05; i=$((i+1)); done; [ -e release ]')
            fg_cmd = (
                'i=0; while [ ! -e bg-ready ] && [ $i -lt 3000 ]; '
                'do sleep 0.05; i=$((i+1)); done; [ -e bg-ready ] || exit 1; '
                'echo FG > "$HOME/fg.txt"; ls "$HOME"; : > release')

            async def _scenario() -> tuple[dict[str, object], dict[str, object]]:
                reg = get_registry()
                try:
                    started = json.loads(await reg["bash"].async_invoke(
                        ctx, {"command": bg_cmd, "run_in_background": True}))
                    fg = json.loads(await reg["bash"].async_invoke(
                        ctx, {"command": fg_cmd}))
                    done = json.loads(await reg["bash_output"].async_invoke(
                        ctx, {"id": started["id"], "wait": True}))
                    return fg, done
                finally:
                    await terminate_background_processes(ctx)

            fg, done = anyio.run(_scenario)

            # both commands succeeded — and the foreground rc 0 means it SAW
            # bg-ready, so the background mount was live for the whole fg call.
            assert fg["returncode"] == 0, f"foreground call failed: {fg}"
            assert done["status"] == "exited", f"background unfinished: {done}"
            assert done["returncode"] == 0, f"background call failed: {done}"

            # one layer per namespace: the still-running background command's
            # $HOME write is visible to the concurrent foreground call.
            listed = str(fg["stdout"]).split()
            assert "fg.txt" in listed, f"foreground $HOME listing: {listed}"
            assert "bg.txt" in listed, f"the two calls did not share a layer: {listed}"

            # AC 14c by observable consequence: prepare_spawn_policy mkdtemps a
            # layer per CALL, and its only tool-path caller is now _open_once —
            # so two concurrently-live spawns leave exactly ONE, holding both
            # writes. This is the assertion that would catch a regression to
            # per-spawn minting; `listed` above would still pass on two layers
            # if the mounts happened to share an upper.
            layers = sorted(
                p for p in Path(pol.overlay_root.path).iterdir() if p.is_dir())
            assert len(layers) == 1, f"expected one layer per namespace, got {layers}"
            contents = sorted(
                sorted(f.name for f in (d / "upper").iterdir()) for d in layers)
            assert contents == [["bg.txt", "fg.txt"]], contents

            # the lowerdir — the real home — saw neither write.
            assert sorted(p.name for p in home.iterdir()) == [], "real home mutated"
        finally:
            cleanup()
    finally:
        shutil.rmtree(root, ignore_errors=True)


@requires_overlay
def test_overlay_home_caches_write_through_to_real_home() -> None:
    # AC 4: a cache dir bound rw ON TOP of the overlay writes THROUGH to the real
    # home (not captured in the discarded upper), so cache downloads persist across
    # runs. Falsifier: in the SAME step a non-cache $HOME write is captured by the
    # overlay (absent from the real home), while the .cache write lands on the real
    # home — distinguishing genuine write-through from a whole-home leak.
    # (The isolated/spiker policy uses the same universal caches — unit-tested by
    # Part 1's test_resolve_policy_disposable_tree_uses_universal_home_model.)
    root = _make_overlay_root()
    try:
        home = root / "home"
        cache = home / ".cache"
        cache.mkdir(parents=True)
        pol, cleanup = _overlay_home(root, home, writable_caches=(str(cache),))
        try:
            ctx = _ctx(Path(pol.worktree), pol)
            r = _bash(
                ctx,
                'echo DL > "$HOME/.cache/pkg.bin"; '
                'echo CAP > "$HOME/captured.txt"; echo done')
            assert r["returncode"] == 0 and "done" in r["stdout"]
            # write-through: the cache file lands on the REAL home cache dir.
            assert (cache / "pkg.bin").exists(), (
                "cache write did not pass through to the real home")
            assert (cache / "pkg.bin").read_text().strip() == "DL"
            # distinguisher: the non-cache write was captured by the overlay upper,
            # so it is ABSENT from the real home (proves this is not a whole-home leak).
            assert not (home / "captured.txt").exists()
        finally:
            cleanup()
    finally:
        shutil.rmtree(root, ignore_errors=True)


@requires_userxattr_overlay
def test_overlay_home_recreating_a_lower_dir_succeeds() -> None:
    # Spec AC 1: removing and recreating a directory that exists in the overlay's
    # LOWER home makes the kernel mark the new upper dir opaque. Unprivileged, it
    # cannot write trusted.overlay.opaque, so without -o userxattr ovl_set_opaque()
    # returns EIO and the mkdir fails. With userxattr the marker goes to
    # user.overlay.opaque and the sequence succeeds.
    root = _make_overlay_root()
    try:
        home = root / "home"
        proj = home / "proj"
        proj.mkdir(parents=True)
        pol, cleanup = _overlay_home(root, home)
        try:
            ctx = _ctx(Path(pol.worktree), pol)
            r = _bash(
                ctx,
                'rmdir "$HOME/proj" && echo REMOVED && '
                'mkdir "$HOME/proj" && echo RECREATED && '
                'echo FRESH > "$HOME/proj/new.txt" && cat "$HOME/proj/new.txt"')
            out, err = str(r["stdout"]), str(r["stderr"])
            assert "Input/output error" not in out + err, (
                f"overlay opaque marker still unwritable: {out!r} {err!r}")
            assert r["returncode"] == 0, f"recreate sequence failed: {r}"
            assert "REMOVED" in out and "RECREATED" in out
            # the recreated dir is usable, not a half-built inode
            assert "FRESH" in out
            # falsifier: this all happened in the discarded upper — the real home's
            # dir is untouched and never saw the new file.
            assert proj.is_dir()
            assert not (proj / "new.txt").exists()
        finally:
            cleanup()
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_fallback_home_is_writable_real_home_with_masks() -> None:
    # AC 8: when overlay is unavailable the home is a plain rw bind of the REAL home
    # — a $HOME write persists to the real home (home isolation is lost, the spec's
    # deliberate degrade-security-not-functionality edge) — and the secret masks
    # STILL apply (secrets masked in BOTH modes). Runs on any bwrap host (no overlay needed).
    root = _make_root("imp-test-fb-")
    try:
        home = root / "home"
        ssh = home / ".ssh"
        ssh.mkdir(parents=True)
        (ssh / "id_rsa").write_text("SUPER_SECRET_KEY\n")

        pol = _fallback_home(root, home, secret_dirs=(str(ssh),))
        ctx = _ctx(root, pol)
        r = _bash(
            ctx,
            'echo FB > "$HOME/fallback.txt" && cat "$HOME/fallback.txt"; '
            'echo "==S=="; cat "$HOME/.ssh/id_rsa" 2>&1')
        out = r["stdout"]
        assert "FB" in out                            # writable home (functionality kept)
        assert "SUPER_SECRET_KEY" not in out          # AC8/AC3: masks apply in fallback
        # rw bind (no overlay capture) → the write persisted to the REAL home.
        assert (home / "fallback.txt").exists(), (
            "fallback rw home did not persist the write to the real home")
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_readonly_overlay_blocks_bash_write_to_tree() -> None:
    # AC 9: a read-only overlay over the tree (what a subagent inherits from a
    # read_only:["."] step — object-level inheritance is covered by
    # test_non_isolated_subagent_inherits_parent_policy in the dispatch suite) blocks
    # a real bash WRITE at the OS level: the last-covering --ro-bind wins over the
    # earlier rw worktree bind.
    root = _make_root("imp-test-ro-")
    try:
        base = _active_policy(root)
        pol = dataclasses.replace(base, readonly_overlays=(str(root),))
        ctx = _ctx(root, pol)
        r = _bash(ctx, f'touch "{root}/blocked.txt" 2>&1; echo rc=$?')
        assert "rc=0" not in r["stdout"], (
            f"write to a read-only-overlaid tree unexpectedly succeeded: "
            f"{r['stdout']!r}")
        assert not (root / "blocked.txt").exists(), (
            "blocked.txt appeared on host — the read-only overlay did not block it")
    finally:
        shutil.rmtree(root, ignore_errors=True)


# ---------------------------------------------------------------------------
# B10.10  Quest steps/ subtree + events.jsonl masked; artifacts/ stays readable
# ---------------------------------------------------------------------------

def test_quest_steps_masked_but_artifacts_readable() -> None:
    """steps/ + events.jsonl masked; artifacts/ stays readable inside the sandbox."""
    control = Path(tempfile.mkdtemp(prefix="imp-test-steps-mask-"))
    try:
        _init_repo(control)
        wt_path = wt.create_worktree(control, "q1", "main")

        state = project_state_dir(control)
        quest_dir = state / "quests" / "q1"
        steps_phase = quest_dir / "steps" / "phase1"
        steps_phase.mkdir(parents=True)
        (steps_phase / "SECRET.txt").write_text("OVERLAY_SENTINEL\n")

        artifacts_dir = quest_dir / "artifacts"
        artifacts_dir.mkdir(parents=True)
        (artifacts_dir / "spec.md").write_text("ARTIFACT_OK\n")

        (quest_dir / "events.jsonl").write_text('{"e":1}\n')

        pol = resolve_policy(
            worktree=wt_path,
            control_root=control,
            quest_id="q1",
            cfg=SandboxConfig(home=control / "home", uid=os.getuid()),
            engine_env_result={"IMP_PROJECT_ROOT": str(wt_path)},
            environ=os.environ,
            available=True,
            quest_dir=quest_dir,
        )
        ctx = ToolContext(
            project_root=wt_path,
            events_path=wt_path / "events.jsonl",
        )
        ctx.quest_context = QuestContext(
            title="Steps Mask Test",
            project_root=wt_path,
            workflow_dir=wt_path / ".imp" / "wf",
        )
        ctx.base_sandbox_policy = pol
        own_tool_context(ctx)

        # steps/ sentinel must NOT be visible (tmpfs masks it, dir appears empty)
        r1 = _bash(ctx, f"cat {steps_phase}/SECRET.txt 2>&1; echo rc=$?")
        assert "OVERLAY_SENTINEL" not in r1["stdout"], (
            f"steps/ sentinel visible inside sandbox — tmpfs mask not applied: {r1['stdout']!r}"
        )
        r2 = _bash(ctx, f"ls {quest_dir}/steps 2>&1")
        assert "phase1" not in r2["stdout"], (
            f"steps/phase1 visible inside sandbox — tmpfs mask not applied: {r2['stdout']!r}"
        )

        # artifacts/ must remain readable; events.jsonl must be masked
        r3 = _bash(ctx, f"cat {artifacts_dir}/spec.md")
        assert "ARTIFACT_OK" in r3["stdout"], (
            f"artifacts/spec.md not readable inside sandbox: {r3!r}"
        )
        r4 = _bash(ctx, f"cat {quest_dir}/events.jsonl")
        assert '{"e":1}' not in r4["stdout"], (
            f"events.jsonl content leaked inside sandbox — /dev/null mask not applied: {r4!r}"
        )
    finally:
        shutil.rmtree(control, ignore_errors=True)


# ---------------------------------------------------------------------------
# B10.11  Siblings + archive invisible in-sandbox; own home read/write (AC 8)
# ---------------------------------------------------------------------------

def test_siblings_and_archive_invisible_own_home_rw() -> None:
    """Normal quest (nested worktree): a sibling quest and the archive are invisible
    inside the sandbox (the state tmpfs), while the current quest reads AND writes its
    own home over the re-bound quest_home."""
    control = Path(tempfile.mkdtemp(prefix="imp-test-imp-mask-"))
    try:
        _init_repo(control)
        wt_path = wt.create_worktree(control, "q1", "main")

        state = project_state_dir(control)
        quest_dir = state / "quests" / "q1"
        spec_dir = quest_dir / "artifacts" / "spec"
        spec_dir.mkdir(parents=True)
        (spec_dir / "q1.md").write_text("OWNSPEC body\n")
        (quest_dir / "events.jsonl").write_text('{"e":1}\n')

        sib = state / "quests" / "sib"
        sib.mkdir(parents=True)
        (sib / "events.jsonl").write_text("SIBSECRET\n")

        archive = state / "archive"
        (archive / "blobs" / "ab").mkdir(parents=True)
        (archive / "blobs" / "ab" / "cd").write_text("ARCHSECRET\n")
        (archive / "records").mkdir()
        (archive / "records" / "r.json").write_text("ARCHREC\n")

        pol = resolve_policy(
            worktree=wt_path, control_root=control, quest_id="q1",
            cfg=SandboxConfig(home=control / "home", uid=os.getuid()),
            engine_env_result={"IMP_PROJECT_ROOT": str(wt_path)},
            environ=os.environ, available=True, quest_dir=quest_dir,
        )
        ctx = _ctx(wt_path, pol)

        # Sibling + archive secrets must not be reachable on disk in-sandbox.
        scan = _bash(
            ctx,
            f"grep -rn SIBSECRET {state} 2>/dev/null; "
            f"grep -rn ARCHSECRET {state} 2>/dev/null; echo SCAN_DONE")
        assert "SCAN_DONE" in scan["stdout"]
        assert "SIBSECRET" not in scan["stdout"], (
            f"sibling events.jsonl leaked inside sandbox: {scan['stdout']!r}")
        assert "ARCHSECRET" not in scan["stdout"], (
            f"archive blob leaked inside sandbox: {scan['stdout']!r}")

        # The quests/ dir shows only the current quest's re-bound home, not the sibling.
        r_ls = _bash(ctx, f"ls {state}/quests 2>&1")
        assert "q1" in r_ls["stdout"], (
            f"own quest home not visible inside sandbox: {r_ls['stdout']!r}")
        assert "sib" not in r_ls["stdout"], (
            f"sibling quest dir visible inside sandbox: {r_ls['stdout']!r}")

        # Own home is fully functional: read the spec, then write a new artifact.
        r_read = _bash(ctx, f"cat {spec_dir}/q1.md")
        assert "OWNSPEC" in r_read["stdout"], (
            f"own spec not readable inside sandbox: {r_read!r}")
        r_write = _bash(
            ctx,
            f"echo WROTE > {quest_dir}/artifacts/new.md && "
            f"cat {quest_dir}/artifacts/new.md")
        assert "WROTE" in r_write["stdout"], (
            f"own artifact write failed inside sandbox: {r_write!r}")
        # The rw quest_home bind writes through: the artifact persists on the host.
        assert (quest_dir / "artifacts" / "new.md").read_text() == "WROTE\n"
    finally:
        shutil.rmtree(control, ignore_errors=True)


# ---------------------------------------------------------------------------
# B10.12  Draft/degenerate agent (worktree == control_root) masks correctly (AC 9)
# ---------------------------------------------------------------------------

def test_draft_agent_masks_siblings_and_archive_reads_writes_own_spec() -> None:
    """Draft/degenerate case (worktree == control_root): the control-root rw bind is
    an ANCESTOR of .imp, so without the tmpfs AFTER it every sibling would leak. This
    confirms the sibling + archive are invisible in-sandbox while the draft agent
    still reads and writes its own artifacts/spec/<id>.md over the quest_home rebind."""
    control = Path(tempfile.mkdtemp(prefix="imp-test-imp-mask-draft-"))
    try:
        _init_repo(control)

        state = project_state_dir(control)
        quest_dir = state / "quests" / "draft1"
        spec_dir = quest_dir / "artifacts" / "spec"
        spec_dir.mkdir(parents=True)
        (spec_dir / "draft1.md").write_text("DRAFTSPEC body\n")
        (quest_dir / "events.jsonl").write_text('{"e":1}\n')

        sib = state / "quests" / "sib"
        sib.mkdir(parents=True)
        (sib / "events.jsonl").write_text("DSIBSECRET\n")

        archive = state / "archive"
        (archive / "blobs" / "ab").mkdir(parents=True)
        (archive / "blobs" / "ab" / "cd").write_text("DARCHSECRET\n")

        pol = resolve_policy(
            worktree=control, control_root=control, quest_id="draft1",
            cfg=SandboxConfig(home=control / "home", uid=os.getuid()),
            engine_env_result={"IMP_PROJECT_ROOT": str(control)},
            environ=os.environ, available=True, quest_dir=quest_dir,
        )
        ctx = _ctx(control, pol)

        scan = _bash(
            ctx,
            f"grep -rn DSIBSECRET {state} 2>/dev/null; "
            f"grep -rn DARCHSECRET {state} 2>/dev/null; echo SCAN_DONE")
        assert "SCAN_DONE" in scan["stdout"]
        assert "DSIBSECRET" not in scan["stdout"], (
            f"draft: sibling events.jsonl leaked in-sandbox: {scan['stdout']!r}")
        assert "DARCHSECRET" not in scan["stdout"], (
            f"draft: archive blob leaked in-sandbox: {scan['stdout']!r}")

        r_ls = _bash(ctx, f"ls {state}/quests 2>&1")
        assert "draft1" in r_ls["stdout"], (
            f"draft: own quest home not visible in-sandbox: {r_ls['stdout']!r}")
        assert "sib" not in r_ls["stdout"], (
            f"draft: sibling quest dir visible in-sandbox: {r_ls['stdout']!r}")

        r_read = _bash(ctx, f"cat {spec_dir}/draft1.md")
        assert "DRAFTSPEC" in r_read["stdout"], (
            f"draft: own spec not readable in-sandbox: {r_read!r}")
        r_write = _bash(
            ctx,
            f"echo DWROTE > {spec_dir}/new.md && cat {spec_dir}/new.md")
        assert "DWROTE" in r_write["stdout"], (
            f"draft: own spec write failed in-sandbox: {r_write!r}")
        # The rw quest_home bind writes through: the artifact persists on the host.
        assert (spec_dir / "new.md").read_text() == "DWROTE\n"
    finally:
        shutil.rmtree(control, ignore_errors=True)


# ---------------------------------------------------------------------------
# B10.13  Other projects readable read-only; own siblings still hidden (AC 10)
# ---------------------------------------------------------------------------

def test_other_projects_state_is_readable_but_never_writable() -> None:
    """Another project's state dir under IMP_HOME/projects reads inside the sandbox
    and refuses writes, while THIS project's sibling quests stay invisible."""
    control = Path(tempfile.mkdtemp(prefix="imp-test-xproject-"))
    try:
        _init_repo(control)
        wt_path = wt.create_worktree(control, "q1", "main")

        state = project_state_dir(control)
        quest_dir = state / "quests" / "q1"
        quest_dir.mkdir(parents=True)

        other = imp_home() / "projects" / "other-repo" / "quests" / "zzz"
        other.mkdir(parents=True)
        (other / "events.jsonl").write_text("OTHERPROJECT\n")

        sib = state / "quests" / "sib"
        sib.mkdir(parents=True)
        (sib / "events.jsonl").write_text("SIBSECRET\n")

        pol = resolve_policy(
            worktree=wt_path, control_root=control, quest_id="q1",
            cfg=SandboxConfig(home=control / "home", uid=os.getuid()),
            engine_env_result={"IMP_PROJECT_ROOT": str(wt_path)},
            environ=os.environ, available=True, quest_dir=quest_dir,
        )
        ctx = _ctx(wt_path, pol)

        r_read = _bash(ctx, f"cat {other}/events.jsonl")
        assert "OTHERPROJECT" in r_read["stdout"], (
            f"other project's event log not readable in-sandbox: {r_read!r}")

        # No exit code asserted: a redirect failure is 1 under bash, 2 under dash.
        r_write = _bash(ctx, f"echo NOPE > {other}/events.jsonl")
        assert "Read-only file system" in r_write["stderr"], (
            f"write to another project's state was not refused in-sandbox: {r_write!r}")
        assert (other / "events.jsonl").read_text() == "OTHERPROJECT\n"

        scan = _bash(ctx, f"grep -rn SIBSECRET {state} 2>/dev/null; echo SCAN_DONE")
        assert "SCAN_DONE" in scan["stdout"]
        assert "SIBSECRET" not in scan["stdout"], (
            f"sibling events.jsonl leaked inside sandbox: {scan['stdout']!r}")

        r_ls = _bash(ctx, f"ls {state}/quests 2>&1")
        assert "q1" in r_ls["stdout"], (
            f"own quest home not visible inside sandbox: {r_ls['stdout']!r}")
        assert "sib" not in r_ls["stdout"], (
            f"sibling quest dir visible inside sandbox: {r_ls['stdout']!r}")
    finally:
        shutil.rmtree(control, ignore_errors=True)


# ---------------------------------------------------------------------------
# The $IMP_STATE_DIR ownership marker survives the state tmpfs, read-only
# ---------------------------------------------------------------------------

def test_repo_marker_readable_but_never_writable_in_sandbox() -> None:
    """AC 23: the guard reads <state dir>/repo, and the state tmpfs would otherwise hide
    it — so an in-sandbox `imp` pointed at a foreign checkout's state dir would read
    clean and write into it. Bind ordering cannot prove visibility; this drives bwrap."""
    control = Path(tempfile.mkdtemp(prefix="imp-test-repo-marker-"))
    try:
        _init_repo(control)
        wt_path = wt.create_worktree(control, "q1", "main")

        state = project_state_dir(control)
        quest_dir = state / "quests" / "q1"
        quest_dir.mkdir(parents=True)
        marker = state / "repo"
        expected = f"{control.resolve()}\n"
        assert marker.read_text() == expected      # minted by the slug walk

        sib = state / "quests" / "sib"
        sib.mkdir(parents=True)
        (sib / "events.jsonl").write_text("MARKERSIBSECRET\n")

        pol = resolve_policy(
            worktree=wt_path, control_root=control, quest_id="q1",
            cfg=SandboxConfig(home=control / "home", uid=os.getuid()),
            engine_env_result={"IMP_PROJECT_ROOT": str(wt_path)},
            environ=os.environ, available=True, quest_dir=quest_dir,
        )
        ctx = _ctx(wt_path, pol)

        r_read = _bash(ctx, f"cat {marker}")
        assert r_read["returncode"] == 0, (
            f"repo marker not readable inside sandbox: {r_read!r}")
        assert r_read["stdout"].strip() == expected.strip(), (
            f"in-sandbox repo marker differs from the host file: {r_read!r}")

        # No exit code asserted: a redirect failure is 1 under bash, 2 under dash.
        r_write = _bash(ctx, f"echo /tmp/elsewhere > {marker}")
        assert "Read-only file system" in r_write["stderr"], (
            f"repo marker was writable inside sandbox: {r_write!r}")
        assert marker.read_text() == expected

        # The re-expose is ONE file: the tmpfs still hides every sibling quest.
        scan = _bash(
            ctx, f"grep -rn MARKERSIBSECRET {state} 2>/dev/null; echo SCAN_DONE")
        assert "SCAN_DONE" in scan["stdout"]
        assert "MARKERSIBSECRET" not in scan["stdout"], (
            f"sibling leaked through the state mask: {scan['stdout']!r}")
    finally:
        shutil.rmtree(control, ignore_errors=True)


def test_slash_tmp_is_the_real_host_tmp() -> None:
    """AC3: /tmp inside the sandbox is the real host /tmp (identity bind); a write
    to /tmp/<name> inside the sandbox appears at the same path on the host."""
    root = _make_root()
    marker = f"beh-{uuid.uuid4().hex}"
    host_path = Path("/tmp") / marker
    try:
        pol = _active_policy(root)
        ctx = _ctx(root, pol)
        r = _bash(ctx, f"echo hi > /tmp/{marker} && echo ok")
        assert r["returncode"] == 0, r
        assert host_path.read_text().strip() == "hi"
    finally:
        host_path.unlink(missing_ok=True)
        shutil.rmtree(root, ignore_errors=True)


# ---------------------------------------------------------------------------
# B10.13  Per-quest /var/tmp: writable, isolated from host root, overlay-capable
# ---------------------------------------------------------------------------

@pytest.mark.skipif(not os.access("/var/tmp", os.W_OK), reason="requires writable /var/tmp")
def test_var_tmp_writable_and_per_quest_isolated() -> None:
    """AC1: inside the sandbox /var/tmp is writable and IS the per-quest source —
    the host's real /var/tmp root is not visible, and in-sandbox writes land in the
    per-quest source dir, not the host root."""
    root = _make_root()
    qvtmp = Path(tempfile.mkdtemp(prefix="imp-qvtmp-", dir="/var/tmp"))
    host_marker = Path("/var/tmp") / f"host-{uuid.uuid4().hex}"
    in_marker = f"beh-{uuid.uuid4().hex}"
    try:
        host_marker.write_text("host\n")   # sits in the real /var/tmp root
        pol = dataclasses.replace(_active_policy(root), quest_var_tmp_dir=str(qvtmp))
        ctx = _ctx(root, pol)
        r = _bash(
            ctx,
            f"echo hi > /var/tmp/{in_marker} && "
            f"( test -e /var/tmp/{host_marker.name} "
            f"&& echo HOSTVISIBLE || echo hostmasked )")
        assert r["returncode"] == 0, r
        assert "hostmasked" in r["stdout"]        # host /var/tmp root shadowed inside
        assert "HOSTVISIBLE" not in r["stdout"]
        # the in-sandbox write landed in the per-quest source, NOT the host root
        assert (qvtmp / in_marker).read_text().strip() == "hi"
        assert not (Path("/var/tmp") / in_marker).exists()
    finally:
        shutil.rmtree(root, ignore_errors=True)
        shutil.rmtree(qvtmp, ignore_errors=True)
        host_marker.unlink(missing_ok=True)


@requires_overlay
def test_var_tmp_supports_overlay_upperdir() -> None:
    """An unprivileged overlay upperdir mounts successfully under /var/tmp inside
    the sandbox — the reason /var/tmp (a real disk fs) is bound rather than tmpfs
    /tmp, which many kernels reject as an overlay upperdir."""
    root = _make_root()
    qvtmp = Path(tempfile.mkdtemp(prefix="imp-qvtmp-ovl-", dir="/var/tmp"))
    try:
        pol = dataclasses.replace(_active_policy(root), quest_var_tmp_dir=str(qvtmp))
        ctx = _ctx(root, pol)
        r = _bash(
            ctx,
            "d=/var/tmp/ovl; mkdir -p $d/l $d/u $d/w $d/m && "
            "unshare --user --map-root-user --mount sh -c '"
            "mount -t overlay overlay -o "
            "lowerdir=/var/tmp/ovl/l,upperdir=/var/tmp/ovl/u,workdir=/var/tmp/ovl/w "
            "/var/tmp/ovl/m && echo OVL_OK || echo OVL_FAIL'")
        assert r["returncode"] == 0, r
        assert "OVL_OK" in r["stdout"], r        # upperdir under /var/tmp mounted
    finally:
        shutil.rmtree(root, ignore_errors=True)
        shutil.rmtree(qvtmp, ignore_errors=True)


def test_worktrees_mask_sibling_survives_in_sandbox_prune() -> None:
    """Incident shape: with A's policy resolved, an in-sandbox `git worktree prune`
    in A's worktree leaves sibling B's .git/worktrees/B admin dir intact, A's own
    `git worktree list` shows only main + A, and an in-sandbox commit in A lands."""
    control = Path(tempfile.mkdtemp(prefix="imp-test-wtmask-"))
    try:
        _init_repo(control)
        wt_a = wt.create_worktree(control, "A", "main")
        wt.create_worktree(control, "B", "main")
        quest_dir = project_state_dir(control) / "quests" / "A"
        quest_dir.mkdir(parents=True)
        sib_admin = control / ".git" / "worktrees" / "B"
        assert sib_admin.is_dir(), "sibling B admin dir missing — setup error"

        pol = resolve_policy(
            worktree=wt_a, control_root=control, quest_id="A",
            cfg=SandboxConfig(home=control / "home", uid=os.getuid()),
            engine_env_result={"IMP_PROJECT_ROOT": str(wt_a)},
            environ=os.environ, available=True, quest_dir=quest_dir,
        )
        ctx = ToolContext(project_root=wt_a, events_path=wt_a / "events.jsonl")
        ctx.quest_context = QuestContext(
            title="Prune Test", project_root=wt_a,
            workflow_dir=wt_a / ".imp" / "wf")
        ctx.base_sandbox_policy = pol
        own_tool_context(ctx)

        # (a) an in-sandbox prune must NOT reap sibling B (it is hidden, not orphaned).
        r_prune = _bash(ctx, "git worktree prune 2>&1; echo rc=$?")
        assert "rc=0" in str(r_prune["stdout"]), r_prune
        assert sib_admin.is_dir(), (
            f"sibling B admin dir deleted by in-sandbox prune: {r_prune}")

        # (b) `git worktree list` succeeds and shows only main + self (B absent).
        r_list = _bash(ctx, "git worktree list 2>&1; echo rc=$?")
        out = str(r_list["stdout"])
        assert "rc=0" in out, r_list
        assert "worktrees/A" in out and "worktrees/B" not in out, r_list

        # self's git ops still work: commit + rebase autosquash + rev-parse tree.
        # GIT_SEQUENCE_EDITOR=true keeps the todo list unchanged so `rebase -i`
        # runs non-interactively; each step is &&-chained so OPS_OK requires ALL
        # three to succeed (a masked-away self admin dir would break the commit).
        r_commit = _bash(
            ctx,
            "echo x > f.txt && git add f.txt && "
            "git -c user.email=t@t -c user.name=t commit -qm w && "
            "GIT_SEQUENCE_EDITOR=true GIT_EDITOR=true "
            "git -c user.email=t@t -c user.name=t rebase -i --autosquash HEAD~1 "
            "  >/dev/null 2>&1 && "
            "git rev-parse HEAD^{tree} >/dev/null 2>&1 && echo OPS_OK")
        assert "OPS_OK" in str(r_commit["stdout"]), r_commit
        assert "w" in _git(wt_a, "log", "--oneline").stdout, "commit not on host"
    finally:
        for q in ("A", "B"):
            with contextlib.suppress(Exception):
                wt.remove_worktree(control, q, force=True)
        shutil.rmtree(control, ignore_errors=True)


def test_worktrees_mask_draft_concierge_prune_spares_siblings() -> None:
    """Draft concierge (worktree == control_root, quest_dir=None): the mask is
    emitted with NO self re-expose, and an in-sandbox `git worktree prune` in the
    main checkout still leaves every sibling admin dir intact — the concierge
    prune vector is closed too."""
    control = Path(tempfile.mkdtemp(prefix="imp-test-wtdraft-"))
    try:
        _init_repo(control)
        wt.create_worktree(control, "B", "main")
        sib_admin = control / ".git" / "worktrees" / "B"
        assert sib_admin.is_dir(), "sibling B admin dir missing — setup error"

        pol = resolve_policy(
            worktree=control, control_root=control, quest_id="draft",
            cfg=SandboxConfig(home=control / "home", uid=os.getuid()),
            engine_env_result={"IMP_PROJECT_ROOT": str(control)},
            environ=os.environ, available=True, quest_dir=None,
        )
        assert pol.git_worktrees_mask == str(control / ".git" / "worktrees")
        assert pol.git_worktree_self == ""
        ctx = ToolContext(project_root=control, events_path=control / "events.jsonl")
        ctx.quest_context = QuestContext(
            title="Draft Prune", project_root=control,
            workflow_dir=control / ".imp" / "wf")
        ctx.base_sandbox_policy = pol
        own_tool_context(ctx)

        r_prune = _bash(ctx, "git worktree prune 2>&1; echo rc=$?")
        assert "rc=0" in str(r_prune["stdout"]), r_prune
        assert sib_admin.is_dir(), (
            f"sibling B admin dir deleted by draft-concierge prune: {r_prune}")
    finally:
        with contextlib.suppress(Exception):
            wt.remove_worktree(control, "B", force=True)
        shutil.rmtree(control, ignore_errors=True)


def test_bash_tool_teardown_lets_a_sigterm_trap_finish() -> None:
    """Interrupting the bash tool leaves a sandboxed command its grace: the trap
    sleeps 0.3s and only then writes a marker into the worktree, host-visible through
    the rw bind. The marker must exist once terminate_running_processes has returned.
    Nothing here asserts elapsed time — the marker is polled — and the teardown is
    held until the command signals readiness, so bwrap startup latency cannot race
    the SIGTERM."""
    root = _make_root()
    ready = root / "up"
    marker = root / "trap-finished"
    ctx = _ctx(root, _active_policy(root))
    out_holder: dict[str, object] = {}

    async def _runner() -> None:
        out_holder["out"] = json.loads(await get_registry()["bash"].async_invoke(
            ctx,
            {"command":
             f"trap 'sleep 0.3; touch {shlex.quote(str(marker))}; exit 0' TERM; "
             f"touch {shlex.quote(str(ready))}; "
             "while :; do sleep 0.2; done"}))

    async def _killer() -> None:
        deadline = time.monotonic() + 20.0
        while not ready.exists():
            if time.monotonic() > deadline:
                raise AssertionError("the sandboxed command never signalled readiness")
            await anyio.sleep(0.02)
        # 8.0, not the 4.0 default: ~25x headroom for the trap's 0.3s under a
        # saturated -n auto run. Free on the passing path — teardown returns as soon
        # as the group empties.
        out_holder["killed"] = await terminate_running_processes(ctx, timeout=8.0)

    async def _scenario() -> None:
        with anyio.fail_after(90.0):  # single outer hang-catcher (generous)
            async with anyio.create_task_group() as tg:
                tg.start_soon(_runner)
                tg.start_soon(_killer)

    try:
        anyio.run(_scenario)
        deadline = time.monotonic() + 20.0
        while not marker.exists():
            if time.monotonic() > deadline:
                raise AssertionError(
                    "the SIGTERM trap never completed — the SIGKILL landed before the "
                    "grace window could be used")
            time.sleep(0.02)
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_sandboxed_shell_cannot_see_or_signal_a_host_process() -> None:
    """The hole this quest closes: a host process spawned OUTSIDE the sandbox must be
    invisible to a sandboxed shell (absent from /proc and from ps) and unsignallable
    (kill(2) returns ESRCH), and it must still be alive afterwards. Without the pid
    namespace all three go the other way — the shell sees it, ps lists it, and the
    SIGTERM lands. A host without `ps` degrades to PS_HIDDEN rather than failing: the
    pipeline yields no match either way, and the /proc and kill clauses stand alone."""
    root = _make_root()
    host = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(300)"])
    try:
        pid = host.pid
        ctx = _ctx(root, _active_policy(root))
        out = _bash(ctx, (
            f"[ -d /proc/{pid} ] && echo PROC_VISIBLE || echo PROC_HIDDEN; "
            f"ps -eo pid= | tr -d ' ' | grep -qx {pid} && echo PS_VISIBLE || echo PS_HIDDEN; "
            f"kill -TERM {pid} 2>/dev/null && echo SIGNAL_OK || echo SIGNAL_FAILED; "
            "exit 0"))
        assert str(out["stdout"]).split() == ["PROC_HIDDEN", "PS_HIDDEN", "SIGNAL_FAILED"], out
        assert host.poll() is None, "the sandboxed kill reached a process outside the sandbox"
    finally:
        host.kill()
        host.wait()
        shutil.rmtree(root, ignore_errors=True)


def test_real_child_env_is_exactly_the_paired_env(monkeypatch) -> None:
    """AC 3: with --clearenv gone, the child's own `env` output must be exactly the
    environment paired with the argv — no ambient var from the parent leaks in."""
    monkeypatch.setenv("IMP_TELEGRAM_TOKEN", "canary-42:secret")
    root = _make_root()
    try:
        pol = _active_policy(root)
        spawn = build_sandbox_spawn("env", cwd=str(root), policy=pol,
                                    engine_env={"IMP_QUEST_ID": "abc123"},
                                    environ={})
        result = subprocess.run(spawn.argv, env=spawn.env,
                                capture_output=True, text=True)
        assert result.returncode == 0, result.stderr
        observed = dict(
            line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
        assert observed["IMP_QUEST_ID"] == "abc123"
        for k, v in spawn.env.items():
            assert observed[k] == v, f"{k} did not reach the child intact"
        # Only the shell's own additions may appear beyond the paired env.
        assert set(observed) - set(spawn.env) <= {"PWD", "SHLVL", "_"}
        assert "IMP_TELEGRAM_TOKEN" not in observed
    finally:
        shutil.rmtree(root, ignore_errors=True)


# ---------------------------------------------------------------------------
# Artifact-ref reads from a read_only:["."] step (denu58)
# ---------------------------------------------------------------------------

def test_artifact_ref_history_readable_from_readonly_sandboxed_worktree() -> None:
    """A read_only:["."] agent (plan-reviewer) can read the plan file's own revision
    history out of refs/imp/artifacts/<id> from inside its sandboxed worktree: the
    git common dir is bound rw and only .git/worktrees plus the ro-remasked
    crown jewels are covered, so the ref and its objects stay readable.

    Falsifiability: mask refs/imp/artifacts or the object store and both git
    commands exit non-zero with empty stdout.
    """
    control, wt_path = _refprot_repo("imp-test-artref-")
    srcdir = Path(tempfile.mkdtemp(prefix="imp-test-artref-src-"))
    try:
        src = srcdir / "esc1-plan-1.md"

        src.write_text("# Plan\n\nrevision one\n")
        assert wt.artifact_commit(
            control, "esc1", {"plans/esc1-plan-1.md": src}, message="plan 1") is True
        src.write_text("# Plan\n\nrevision two\n")
        assert wt.artifact_commit(
            control, "esc1", {"plans/esc1-plan-1.md": src},
            message="plan 1 revised") is True

        ctx, pol = _refprot_ctx(control, wt_path)
        # What _apply_readonly_overlay composes for a read_only: ["."] step: the
        # worktree itself overlaid ro.
        ctx.base_sandbox_policy = dataclasses.replace(
            pol, readonly_overlays=(*pol.readonly_overlays, str(wt_path)))

        ref = "refs/imp/artifacts/esc1"
        p = "plans/esc1-plan-1.md"

        # The tree really is read-only for this policy — the probes below run as
        # the reviewer runs, not as a privileged writer.
        blocked = _bash(ctx, f'touch "{wt_path}/nope.txt" 2>&1; echo rc=$?')
        assert "rc=0" not in blocked["stdout"], blocked

        # $IMP_PROJECT_ROOT is the worktree here: the bash tool builds its child
        # env from ctx.project_root.
        oneline = _bash(
            ctx,
            f'git -C "$IMP_PROJECT_ROOT" log --oneline {ref} -- {p}; echo rc=$?')
        assert "rc=0" in oneline["stdout"], (
            f"artifact-ref log failed from inside the sandbox: {oneline}")
        revisions = [ln for ln in oneline["stdout"].splitlines()
                     if ln and not ln.startswith("rc=")]
        assert len(revisions) == 2, (
            f"expected both revisions of {p}, got {revisions!r}")
        assert any("plan 1 revised" in ln for ln in revisions), revisions

        shas = _bash(
            ctx,
            f'git -C "$IMP_PROJECT_ROOT" log --format=%H {ref} -- {p}; echo rc=$?')
        assert "rc=0" in shas["stdout"], shas
        commits = [ln for ln in shas["stdout"].splitlines()
                   if ln and not ln.startswith("rc=")]
        newest, second_newest = commits[0], commits[1]

        delta = _bash(
            ctx,
            f'git -C "$IMP_PROJECT_ROOT" diff {second_newest} {newest} -- {p}; '
            f'echo rc=$?')
        assert "rc=0" in delta["stdout"], (
            f"artifact-ref diff failed from inside the sandbox: {delta}")
        assert "-revision one" in delta["stdout"], delta["stdout"]
        assert "+revision two" in delta["stdout"], delta["stdout"]
    finally:
        with contextlib.suppress(Exception):
            wt.remove_worktree(control, "esc1", force=True)
        shutil.rmtree(control, ignore_errors=True)
        shutil.rmtree(srcdir, ignore_errors=True)


def test_every_tool_context_here_is_owned():
    assert_all_tool_contexts_owned(Path(__file__))


# ---------------------------------------------------------------------------
# join_exec — the exec-shaped join a pty session's argv payload rides on
# ---------------------------------------------------------------------------

_JOIN_RUN_TIMEOUT_S = 30.0   # generous vs the measured ~1ms join; never false-fires

# Every test below opens a real keeper, so each carries its own outer timeout:
# this file's module mark is skipif-only, leaving the project-wide 60s default
# (pyproject.toml), which a keeper open plus a join can legitimately exceed under
# a saturated -n auto run — SandboxNamespace.open retries its spawn up to
# SANDBOX_SPAWN_MAX_ATTEMPTS times with backoffs before _JOIN_RUN_TIMEOUT_S even
# starts. 180s is the single outer hang-catcher, sized generously against the
# ~5ms measured open rather than as an arithmetic bound on the worst case.


def _run_joined(ns: SandboxNamespace, argv: list[str],
                *, cwd: str) -> subprocess.CompletedProcess[str]:
    """Run one argv through `ns.join_exec` headlessly — the same shape a pty
    caller spawns, minus the terminal."""
    spawn = ns.join_exec(argv, cwd=cwd, engine_env={})
    return subprocess.run(spawn.argv, env=spawn.env, pass_fds=spawn.pass_fds,
                          capture_output=True, text=True,
                          timeout=_JOIN_RUN_TIMEOUT_S)


@pytest.mark.timeout(180)
def test_join_exec_resolves_its_cwd_inside_the_namespaces_mount_view() -> None:
    """AC 7(d): the cwd rides on --wdns, which resolves in the TARGET mount
    namespace. --wd= and Popen's cwd= both resolve in the CALLER's, and were
    measured handing the payload the host's view of a --tmpfs-masked directory."""
    root = _make_root()
    masked = root / "masked"
    masked.mkdir()
    (masked / "flag.txt").write_text("REAL-SECRET-CONTENTS-12345\n")
    try:
        ns = SandboxNamespace.open(
            policy=_active_policy(root, secret_dirs=(str(masked),)),
            engine_env={}, environ=os.environ)
        try:
            done = _run_joined(ns, ["sh", "-c", "pwd; ls -A ."], cwd=str(masked))
            assert done.returncode == 0, done.stderr
            lines = done.stdout.splitlines()
            assert lines[0] == str(masked)   # the requested cwd, not a fallback
            assert lines[1:] == []           # the empty tmpfs mask, not flag.txt
        finally:
            ns.close()
    finally:
        shutil.rmtree(root, ignore_errors=True)


@pytest.mark.timeout(180)
def test_a_joined_payload_cannot_unmount_a_secret_mask() -> None:
    """AC 7(c): entering the keeper's own user namespace hands the payload
    CAP_FULL_SET whenever bwrap built a single userns level (the production
    shape), which is enough to umount a secret_dirs tmpfs and read what it hid.
    Dropping join()'s `unshare --user --map-current-user` stage from the exec
    shape would give a driven program a capability `bash` does not have."""
    root = _make_root()
    masked = root / "masked"
    masked.mkdir()
    (masked / "flag.txt").write_text("REAL-SECRET-CONTENTS-12345\n")
    q = shlex.quote(str(masked))
    probe = (
        # Without this guard a missing umount(8) would make the whole test pass
        # vacuously — it would "prove" confinement by never attempting the escape.
        f"command -v umount >/dev/null || {{ echo NO-UMOUNT; exit 1; }}; "
        f"umount {q} 2>&1 | head -1; "
        f"cat {q}/flag.txt 2>&1 | head -1; "
        # Positive, not merely absent: a missing secret proves nothing on its own
        # (any unrelated `cat` failure reads the same). The mask must still BE there
        # afterwards, which for a --tmpfs is an empty listing.
        f'echo "LEFT=[$(ls -A {q})]"; '
        f"echo PROBE-DONE"
    )
    try:
        ns = SandboxNamespace.open(
            policy=_active_policy(root, secret_dirs=(str(masked),)),
            engine_env={}, environ=os.environ)
        try:
            done = _run_joined(ns, ["sh", "-c", probe], cwd=str(root))
            assert "NO-UMOUNT" not in done.stdout, "umount(8) missing; probe was vacuous"
            assert "PROBE-DONE" in done.stdout, done.stdout
            assert "LEFT=[]" in done.stdout, done.stdout   # the mask survived the umount
            assert "REAL-SECRET-CONTENTS-12345" not in done.stdout
        finally:
            ns.close()
    finally:
        shutil.rmtree(root, ignore_errors=True)


@pytest.mark.timeout(180)
def test_a_sandboxed_command_can_still_allocate_a_pty() -> None:
    """The consequence test_nothing_is_bound_between_dev_and_chdir guards by
    argv shape: bwrap's --dev mounts its OWN devpts at /dev/pts and makes
    /dev/ptmx a SYMLINK into it, so anything bound over /dev/pts repoints ptmx.
    On a standard host the host's devpts is ptmxmode=000 and its ptmx is owned by
    real root, unmapped in the sandbox's userns, so openpty() fails for
    everything inside — including imp's own pty tests, which are can_pty()-gated
    and would SKIP rather than fail.

    NESTED (inside imp's own sandbox, where the gates run) this test cannot see
    that regression: the /dev/pts a nested bwrap binds is already bwrap's
    permissive ptmxmode=666 instance, so ptmx stays openable and this stays
    green. The argv assertion is what bites there."""
    root = _make_root()
    prog = "import pty, os\nm, s = pty.openpty()\nprint('PTY=' + os.ttyname(s))\n"
    try:
        ctx = _ctx(root, _active_policy(root))
        result = _bash(ctx, f"{shlex.quote(sys.executable)} -c {shlex.quote(prog)}")
        assert result["returncode"] == 0, result
        assert "PTY=/dev/pts/" in str(result["stdout"]), result
    finally:
        shutil.rmtree(root, ignore_errors=True)


@pytest.mark.timeout(180)
@pytest.mark.skipif(not can_pty(), reason="pty not available")
def test_a_joined_pty_session_keeps_the_exit_status_shape() -> None:
    """AC 10: nsenter faithfully re-raises the child's terminating signal on
    itself, so a joined session's returncode keeps the negative-signal shape the
    providers' tool_killed detection reads — never 128+n, never nsenter's own."""
    root = _make_root()
    cases = [
        ("import sys; sys.exit(7)", 7),
        ("import os, signal; os.kill(os.getpid(), signal.SIGKILL)", -9),
        ("import os, signal; os.kill(os.getpid(), signal.SIGTERM)", -15),
    ]
    try:
        ns = SandboxNamespace.open(policy=_active_policy(root), engine_env={},
                                   environ=os.environ)
        try:
            for prog, expected in cases:
                spawn = ns.join_exec([sys.executable, "-c", prog],
                                     cwd=str(root), engine_env={})
                with TerminalSession(spawn.argv, cwd=None, env=spawn.env,
                                     pass_fds=spawn.pass_fds) as s:
                    assert s.run_to_completion(hard=30.0) == expected, prog
        finally:
            ns.close()
    finally:
        shutil.rmtree(root, ignore_errors=True)


# ---------------------------------------------------------------------------
# The pty session, joined: a driven program is confined exactly as bash is
# ---------------------------------------------------------------------------

_SECRET = "REAL-SECRET-CONTENTS-12345"


def _joined_driver(ctx: ToolContext) -> ImptyDriver:
    """A driver wired to ctx's sandbox, with the hook's verdict CHECKED. Every arm
    below either compares a joined session against an unjoined one or asserts a
    denial; a hook that silently declined would turn the joined side into a second
    bare run and BOTH shapes would then pass vacuously."""
    hook = make_impty_sandbox_hook(ctx)

    def checked(argv, cwd, env):
        plan = hook(argv, cwd, env)
        assert plan is not None and plan.argv[0] == "nsenter", plan
        return plan

    return ImptyDriver(sandbox=checked)


def _drive_joined(ctx: ToolContext, body: Callable[[ImptyDriver], str]) -> str:
    """Run `body` against a joined driver the way a provider does: the hook is
    built ON the run's loop (it captures it) and every driver call is offloaded to
    a worker thread, which is the only shape the loop-affine acquisition supports."""
    async def _main() -> str:
        driver = _joined_driver(ctx)
        try:
            return await asyncio.to_thread(lambda: body(driver))
        finally:
            await asyncio.to_thread(driver.close)

    return asyncio.run(_main())


def _drive_bare(body: Callable[[ImptyDriver], str]) -> str:
    """The same program with NO hook — the unjoined baseline the joined arms are
    measured against, and the control that proves confinement rather than a spawn
    so broken it fails at everything."""
    driver = ImptyDriver()
    try:
        return body(driver)
    finally:
        driver.close()


def _await_screen(driver: ImptyDriver, text: str) -> None:
    """Poll the rendered screen for `text` — the actual signal rather than a fixed
    sleep, on the shared generous budget that cannot false-fire under a saturated
    -n auto run; the per-test outer mark stays the single hang-catcher."""
    _poll(lambda: text in driver.view(), what=f"{text!r} on the screen")


_PEEK = (
    "import os, sys\n"
    "d = sys.argv[1]\n"
    "print('LEFT=' + repr(sorted(os.listdir(d))))\n"
    "try:\n"
    "    print('READ=' + open(os.path.join(d, 'flag.txt')).read().strip())\n"
    "except OSError as exc:\n"
    "    print('READ-DENIED', exc.errno)\n"
)


@pytest.mark.timeout(180)
@pytest.mark.skipif(not can_pty(), reason="pty not available")
def test_a_joined_pty_session_cannot_read_a_masked_secret() -> None:
    """AC 1(a)/(c): a program driven on a pty is confined exactly as a bash call
    is — the --tmpfs mask is in ITS mount view, not the host's. The hook-less arm
    is the control: it reads the real contents, so a green joined arm cannot be a
    broken spawn wearing confinement's clothes."""
    root = _make_root()
    masked = root / "masked"
    masked.mkdir()
    (masked / "flag.txt").write_text(_SECRET + "\n")
    argv = [sys.executable, "-c", _PEEK, str(masked)]
    try:
        ctx = _ctx(root, _active_policy(root, secret_dirs=(str(masked),)))
        joined = _drive_joined(ctx, lambda d: d.start(argv))
        assert "LEFT=[]" in joined, joined              # the empty tmpfs mask
        assert "READ-DENIED 2" in joined, joined        # ENOENT, not a stray error
        assert _SECRET not in joined
        bare = _drive_bare(lambda d: d.start(argv))
        assert "LEFT=['flag.txt']" in bare, bare
        assert f"READ={_SECRET}" in bare, bare
    finally:
        shutil.rmtree(root, ignore_errors=True)


_WRITE_PROBE = (
    "import sys\n"
    "try:\n"
    "    open(sys.argv[1], 'w').write('WROTE')\n"
    "    print('WRITE-OK')\n"
    "except OSError as exc:\n"
    "    print('WRITE-DENIED', exc.errno)\n"
)


@pytest.mark.timeout(180)
@pytest.mark.skipif(not can_pty(), reason="pty not available")
def test_a_joined_pty_session_cannot_write_outside_the_writable_set() -> None:
    """AC 1(b), on the spec's own motivating example: today `start(["git",
    "commit", ...])` in the main checkout is not stopped by the read-only
    .git/index bind the policy applies to every bash call. The target is
    host-WRITABLE, so the unjoined control really does land the write — a /etc
    probe would fail unsandboxed too and prove nothing."""
    root = _make_root()
    try:
        policy = _active_policy(root)
        index = root / ".git" / "index"          # _active_policy created .git
        index.write_text("IDX")
        policy = dataclasses.replace(policy, git_ro_remask=(str(index),))
        argv = [sys.executable, "-c", _WRITE_PROBE, str(index)]
        ctx = _ctx(root, policy)
        joined = _drive_joined(ctx, lambda d: d.start(argv))
        assert "WRITE-DENIED 30" in joined, joined      # EROFS
        assert index.read_text() == "IDX"               # untouched on the host
        bare = _drive_bare(lambda d: d.start(argv))
        assert "WRITE-OK" in bare, bare
        assert index.read_text() == "WROTE"
    finally:
        shutil.rmtree(root, ignore_errors=True)


@pytest.mark.timeout(180)
@pytest.mark.skipif(not can_pty(), reason="pty not available")
def test_sandbox_off_spawns_the_pty_bare_and_unconfined(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC 5(a): with the coordinator unsandboxed the adapter declines and the
    driver spawns the caller's own argv — asserted on the argv the session was
    actually built with, not merely on the hook's return, plus the masked path
    being readable, which is what /sandbox off is FOR."""
    root = _make_root()
    masked = root / "masked"
    masked.mkdir()
    (masked / "flag.txt").write_text(_SECRET + "\n")
    argv = [sys.executable, "-c", _PEEK, str(masked)]
    seen: list[list[str]] = []
    real_init = TerminalSession.__init__

    def spy(self, spawn_argv, **kwargs):
        seen.append(list(spawn_argv))
        real_init(self, spawn_argv, **kwargs)

    monkeypatch.setattr(TerminalSession, "__init__", spy)
    try:
        ctx = _ctx(root, _active_policy(root, secret_dirs=(str(masked),)))
        ctx.coordinator = SimpleNamespace(unsandboxed=True, should_pause=False)

        async def _main() -> str:
            driver = ImptyDriver(sandbox=make_impty_sandbox_hook(ctx))
            try:
                return await asyncio.to_thread(lambda: driver.start(argv))
            finally:
                await asyncio.to_thread(driver.close)

        screen = asyncio.run(_main())
        assert seen == [argv], seen             # the caller's argv, verbatim
        assert "nsenter" not in " ".join(seen[0])
        assert f"READ={_SECRET}" in screen, screen
    finally:
        shutil.rmtree(root, ignore_errors=True)


@pytest.mark.timeout(180)
@pytest.mark.skipif(not can_pty(), reason="pty not available")
def test_toggling_the_sandbox_back_on_rejoins_the_very_same_namespace() -> None:
    """AC 5(b): /sandbox off changes the EFFECTIVE policy, never
    ctx.base_sandbox_policy, and the rebuild rule keys on that base value — so
    toggling back on must find the same keeper object rather than build a second
    one, stranding every process the first still holds."""
    root = _make_root()

    async def _main() -> None:
        ctx = _ctx(root, _active_policy(root))
        ctx.coordinator = SimpleNamespace(unsandboxed=False, should_pause=False)
        hook = make_impty_sandbox_hook(ctx)
        assert await asyncio.to_thread(lambda: hook(["true"], None, None)) is not None
        before = ctx.sandbox_ns
        assert before is not None
        ctx.coordinator.unsandboxed = True
        assert await asyncio.to_thread(lambda: hook(["true"], None, None)) is None
        assert ctx.sandbox_ns is before      # declining tears down nothing
        ctx.coordinator.unsandboxed = False
        assert await asyncio.to_thread(lambda: hook(["true"], None, None)) is not None
        assert ctx.sandbox_ns is before      # rejoined, not rebuilt

    try:
        asyncio.run(_main())
    finally:
        shutil.rmtree(root, ignore_errors=True)


_SLEEPER = "import time\nprint('PTY-READY', flush=True)\ntime.sleep(300)\n"


@pytest.mark.timeout(180)
@pytest.mark.skipif(not can_pty(), reason="pty not available")
def test_teardown_closes_the_pty_session_before_the_namespace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC 8(b): ORDER, not end state. Killing pid 1 of the pid namespace first
    would leave the driver holding a dead child and a corpse screen that still
    renders — today claude_code tears down in exactly that order."""
    root = _make_root()
    order: list[str] = []
    real_ns_close = SandboxNamespace.close

    def spy_ns_close(self: SandboxNamespace) -> None:
        order.append("namespace")
        real_ns_close(self)

    async def _main() -> None:
        ctx = _ctx(root, _active_policy(root))
        driver = attach_impty_driver(ctx)
        real_driver_close = driver.close

        def spy_driver_close() -> None:
            order.append("session")
            real_driver_close()

        screen = await asyncio.to_thread(
            lambda: driver.start([sys.executable, "-c", _SLEEPER]))
        assert "PTY-READY" in screen, screen
        # Spied AFTER the start, so only the teardown's closes are recorded.
        monkeypatch.setattr(driver, "close", spy_driver_close)
        monkeypatch.setattr(SandboxNamespace, "close", spy_ns_close)
        await teardown_tool_context(ctx)

    try:
        asyncio.run(_main())
        assert order == ["session", "namespace"], order
    finally:
        shutil.rmtree(root, ignore_errors=True)


@pytest.mark.timeout(180)
@pytest.mark.skipif(not can_pty(), reason="pty not available")
def test_a_namespace_rebuild_closes_the_pty_session_before_the_stale_namespace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC 9(a): create_and_enter_worktree reassigns base_sandbox_policy, and the
    rebuild branch deliberately does NOT route through close_sandbox_namespace —
    it vacates the slot and closes the object it captured, inline. A single seam
    at the end-of-run site would leave THIS path tearing a namespace down under a
    live session."""
    root = _make_root()
    other = _make_root()
    order: list[str] = []
    real_ns_close = SandboxNamespace.close

    def spy_ns_close(self: SandboxNamespace) -> None:
        order.append("namespace")
        real_ns_close(self)

    async def _main() -> None:
        ctx = _ctx(root, _active_policy(root))
        driver = attach_impty_driver(ctx)
        real_driver_close = driver.close

        def spy_driver_close() -> None:
            order.append("session")
            real_driver_close()

        screen = await asyncio.to_thread(
            lambda: driver.start([sys.executable, "-c", _SLEEPER]))
        assert "PTY-READY" in screen, screen
        first = ctx.sandbox_ns
        monkeypatch.setattr(driver, "close", spy_driver_close)
        monkeypatch.setattr(SandboxNamespace, "close", spy_ns_close)
        ctx.base_sandbox_policy = dataclasses.replace(
            ctx.base_sandbox_policy, worktree=str(other))
        second = await ensure_tool_namespace(ctx, engine_env={},
                                             on_retry=lambda _n, _d: None)
        assert second is not first          # the branch really was taken
        # Deliberately NO trailing driver.close(): the rebuild branch already
        # closed the session, so the only way "session" can reach `order` is if
        # that branch put it there. A courtesy close would route through the same
        # spy, append a third entry, AND hollow out the oracle.

    try:
        asyncio.run(_main())
        assert order == ["session", "namespace"], order
    finally:
        shutil.rmtree(root, ignore_errors=True)
        shutil.rmtree(other, ignore_errors=True)


@pytest.mark.timeout(180)
@pytest.mark.skipif(not can_pty(), reason="pty not available")
def test_after_a_rebuild_the_driver_reports_no_session_and_rejoins_the_new_one() -> None:
    """AC 9(b): the agent gets the ordinary no-session answer rather than a screen
    from a namespace that no longer exists, and a fresh start on the SAME driver
    lands in the new namespace — proven by a bash call through the same context
    seeing it, which is only true if both are in one pid namespace."""
    root = _make_root()
    other = _make_root()
    marker = f"PTY-REBOUND-{uuid.uuid4().hex[:8]}"

    async def _main() -> None:
        ctx = _ctx(root, _active_policy(root))
        driver = attach_impty_driver(ctx)
        await asyncio.to_thread(
            lambda: driver.start([sys.executable, "-c", _SLEEPER]))
        ctx.base_sandbox_policy = dataclasses.replace(
            ctx.base_sandbox_policy, worktree=str(other))
        await ensure_tool_namespace(ctx, engine_env={},
                                    on_retry=lambda _n, _d: None)
        with pytest.raises(RuntimeError, match="no session"):
            await asyncio.to_thread(driver.view)
        screen = await asyncio.to_thread(
            lambda: driver.start([sys.executable, "-c", _SLEEPER, marker]))
        assert "PTY-READY" in screen, screen
        found = json.loads(await get_registry()["bash"].async_invoke(
            ctx, {"command": f"pgrep -f {marker}"}))
        assert found["returncode"] == 0, found
        await asyncio.to_thread(driver.close)

    try:
        asyncio.run(_main())
    finally:
        shutil.rmtree(root, ignore_errors=True)
        shutil.rmtree(other, ignore_errors=True)


_SPAWNS_A_GRANDCHILD = (
    "import subprocess, sys, time\n"
    "subprocess.Popen(['setsid', 'sh', '-c',\n"
    "                  'while :; do sleep 5; done', sys.argv[1] + '-GC'])\n"
    "print('PTY-READY', flush=True)\n"
    "time.sleep(300)\n"
)


@pytest.mark.timeout(180)
@pytest.mark.skipif(not can_pty(), reason="pty not available")
def test_teardown_reaps_a_setsid_grandchild_of_a_pty_program() -> None:
    """AC 8(a): TerminalSession.close() deliberately does not chase grandchildren
    (that contract is unchanged), so today a setsid'd one leaks to the host for
    good — the same stray class the keeper exists to prevent. It is the NAMESPACE
    close that ends it, which is why the two closes must both happen and in this
    order. The grandchild carries its own `-GC` marker so 'still alive after the
    session close' names IT and not the dying pty program."""
    root = _make_root()
    marker = f"PTY-STRAY-{uuid.uuid4().hex[:8]}"
    gc_marker = f"{marker}-GC"

    async def _main() -> None:
        ctx = _ctx(root, _active_policy(root))
        driver = attach_impty_driver(ctx)
        screen = await asyncio.to_thread(
            lambda: driver.start([sys.executable, "-c", _SPAWNS_A_GRANDCHILD, marker]))
        assert "PTY-READY" in screen, screen
        _poll(lambda: bool(_host_pids(gc_marker)), what="the setsid grandchild")
        await asyncio.to_thread(driver.close)
        # The session close alone leaves it — without this the test could pass on
        # a teardown that never needed the namespace at all.
        assert _host_pids(gc_marker), "the session close reaped it; nothing is proven"
        await teardown_tool_context(ctx)
        _poll(lambda: not _host_pids(gc_marker),
              what="the setsid grandchild dying with the namespace")

    try:
        asyncio.run(_main())
    finally:
        for pid in _host_pids(gc_marker):
            with contextlib.suppress(ProcessLookupError, ValueError):
                os.kill(int(pid), signal.SIGKILL)
        shutil.rmtree(root, ignore_errors=True)


_COOKED = "import time\nprint('COOKED-READY', flush=True)\ntime.sleep(300)\n"


@pytest.mark.timeout(180)
@pytest.mark.skipif(not can_pty(), reason="pty not available")
def test_ctrl_c_terminates_a_joined_cooked_mode_program_by_sigint() -> None:
    """AC 2(a): sessions and cttys are not namespaced, so the controlling terminal
    is inherited across nsenter's fork and the preexec TIOCSCTTY still lands. A
    \x03 through the line discipline is therefore a REAL SIGINT, with the same
    status the unjoined baseline reports.

    The death is POLLED, not given a fixed window: a fixed settle that overran
    under a saturated -n auto run would let stop()'s graceful rung SIGTERM a still
    live child, and the test would report `[stopped: exit=SIGTERM]` — a symptom
    that reads like a send_signal regression rather than the timing miss it is."""
    root = _make_root()
    argv = [sys.executable, "-c", _COOKED]

    def _drive(d: ImptyDriver) -> str:
        d.start(argv)
        _await_screen(d, "COOKED-READY")
        d.send("\x03", wait=False)
        _poll(lambda: "alive=False" in d.view(),
              what="the cooked-mode program dying of the tty's SIGINT")
        return d.stop()

    try:
        ctx = _ctx(root, _active_policy(root))
        joined = _drive_joined(ctx, _drive)
        bare = _drive_bare(_drive)
        assert joined.splitlines()[0] == "[stopped: exit=SIGINT]", joined
        assert bare.splitlines()[0] == "[stopped: exit=SIGINT]", bare
    finally:
        shutil.rmtree(root, ignore_errors=True)


_REPORTS_SIZE = (
    "import os, signal, time\n"
    "def show(*_):\n"
    "    s = os.get_terminal_size(0)\n"
    "    os.write(1, f'SIZE={s.columns}x{s.lines}\\r\\n'.encode())\n"
    "signal.signal(signal.SIGWINCH, show)\n"
    "show()\n"
    "time.sleep(300)\n"
)


@pytest.mark.timeout(180)
@pytest.mark.skipif(not can_pty(), reason="pty not available")
def test_resize_reflows_a_joined_program_on_sigwinch() -> None:
    """AC 2(b): the pty is imp's, on the host, and the program holds it across the
    join — so a TIOCSWINSZ reaches it as a real SIGWINCH and it reports the new
    size, exactly as unjoined. Reflow is the whole point of cols/rows."""
    root = _make_root()
    argv = [sys.executable, "-c", _REPORTS_SIZE]

    def _drive(d: ImptyDriver) -> str:
        d.start(argv, cols=80, rows=24)
        _await_screen(d, "SIZE=80x24")
        d.resize(30, 100)
        _await_screen(d, "SIZE=100x30")
        screen = d.view()
        d.stop()
        return screen

    try:
        ctx = _ctx(root, _active_policy(root))
        for screen in (_drive_joined(ctx, _drive), _drive_bare(_drive)):
            assert "SIZE=80x24" in screen, screen     # the pre-resize report
            assert "SIZE=100x30" in screen, screen    # and the reflowed one
    finally:
        shutil.rmtree(root, ignore_errors=True)


_ALT_SCREEN = (
    "import sys, time\n"
    "sys.stdout.write('\\x1b[?1049h\\x1b[H\\x1b[2J')\n"
    "sys.stdout.write('ALT-LINE-ONE\\r\\n  ALT-LINE-TWO')\n"
    "sys.stdout.flush()\n"
    "time.sleep(300)\n"
)


@pytest.mark.timeout(180)
@pytest.mark.skipif(not can_pty(), reason="pty not available")
def test_an_alt_screen_program_renders_identically_joined_and_unjoined() -> None:
    """AC 2(c): lines(), the alt-screen state and the regime — all three carried by
    the driver's own header — must not shift because the program was joined, or a
    joined session would be a different thing to look at than the one the agent's
    skill describes. Measured byte-identical on this host."""
    root = _make_root()
    argv = [sys.executable, "-c", _ALT_SCREEN]

    def _drive(d: ImptyDriver) -> str:
        d.start(argv)
        _await_screen(d, "ALT-LINE-TWO")
        screen = d.view()
        d.stop()
        return screen

    try:
        ctx = _ctx(root, _active_policy(root))
        joined = _drive_joined(ctx, _drive)
        bare = _drive_bare(_drive)
        # Positive first: the program really is on the alt screen, so an equality
        # between two SCROLL-regime screens cannot pass for this.
        assert "regime=screen" in joined, joined
        assert "ALT-LINE-ONE" in joined
        assert joined == bare
    finally:
        shutil.rmtree(root, ignore_errors=True)
