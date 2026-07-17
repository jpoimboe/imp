"""Behavioral bwrap integration tests (spec Part B §7 / B10).

Each test builds a REAL active SandboxPolicy, runs a real bash command under
bwrap, and asserts the OS-level outcome (write-confinement, secret masking,
/tmp persistence, network argv, git commit + escape-vector blocking).

Sandbox worktrees use the default temp directory (/tmp). The real /tmp is
bound rw by identity (--bind /tmp /tmp emitted right after the ro-floor), so
a worktree under /tmp is NOT shadowed inside the namespace — --chdir works
and /tmp files are visible inside and outside the sandbox.
"""
from __future__ import annotations

import contextlib
import dataclasses
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

import anyio
import pytest

import imp.agents.tools.bash  # noqa: F401 — registers the tool
from imp.agents.runner import QuestContext
from imp.agents.tools import get_registry
from imp.agents.tools.context import ToolContext
from imp.engine import worktree as wt
from imp.engine.sandbox import (
    SandboxConfig,
    SandboxPolicy,
    build_sandbox_argv,
    make_home_layer,
    overlay_available,
    resolve_policy,
    sandbox_available,
)

pytestmark = pytest.mark.skipif(
    not sandbox_available(),
    reason="bwrap unavailable",
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_root(prefix: str = "imp-test-beh-") -> Path:
    """Create a sandbox root under the default temp dir (/tmp).

    The real /tmp identity bind means the worktree is not shadowed inside the
    namespace — no /var/tmp workaround needed.
    """
    return Path(tempfile.mkdtemp(prefix=prefix))


def _active_policy(worktree: Path, *, home: Path | None = None,
                   secret_dirs: tuple[str, ...] = ()) -> SandboxPolicy:
    """Minimal hand-built active policy for simple behavioral tests.

    Uses PATH from the real environment so git, cat, echo, etc. resolve.
    HOME is set to a dir under the worktree root (or the provided `home`).
    The real /tmp is bound rw by identity in build_sandbox_argv — no per-test
    tmp dir setup is needed.
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
        setenv=(
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
    return ctx


def _bash(ctx: ToolContext, cmd: str) -> dict[str, object]:
    """Run cmd under ctx's sandbox policy; return parsed JSON {stdout, stderr, returncode}."""
    async def _run() -> str:
        return await get_registry()["bash"].async_invoke(ctx, {"command": cmd})
    return json.loads(anyio.run(_run))


# The overlay home under test must live at a path covered by NO later bind_plan
# bind — NOT under /tmp (build_sandbox_argv emits --bind /tmp /tmp, which re-mounts
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


def _probe_overlay_at_base() -> bool:
    """Probe overlay support at the /var/tmp base, removing the probe dirs after.
    overlay_available() only rmtrees its own inner .overlay-probe subdir; the
    mkdtemp'd probe/home parents are cleaned up here. `home` is a separate empty
    dir (non-overlapping with the probe's scratch layers) so a bare host mounts it
    cleanly — the same real-home lowerdir shape the per-command mount uses."""
    if not sandbox_available() or not _OVERLAY_BASE.is_dir():
        return False
    try:
        probe = Path(tempfile.mkdtemp(prefix="imp-ovl-probe-", dir=_OVERLAY_BASE))
        home = Path(tempfile.mkdtemp(prefix="imp-ovl-home-", dir=_OVERLAY_BASE))
    except OSError:
        return False
    try:
        return overlay_available(probe, home)
    finally:
        shutil.rmtree(probe, ignore_errors=True)
        shutil.rmtree(home, ignore_errors=True)


_OVERLAY_OK = _probe_overlay_at_base()
requires_overlay = pytest.mark.skipif(
    not _OVERLAY_OK,
    reason="unprivileged overlayfs unavailable at a non-/tmp base on this host")


def _make_overlay_root() -> Path:
    """A throwaway base dir OUTSIDE /tmp for the overlay tests (see _OVERLAY_BASE)."""
    return Path(tempfile.mkdtemp(prefix="imp-test-ovl-", dir=_OVERLAY_BASE))


def _overlay_home(base: Path, home: Path, *,
                  secret_dirs: tuple[str, ...] = (),
                  writable_caches: tuple[str, ...] = ()):
    """Active policy with a real COW-overlay HomeLayer over `home`. `home` and the
    sandbox worktree are SIBLINGS under `base` (a non-/tmp real fs), so no later
    bind_plan bind (/tmp identity, worktree rw) covers `home` and shadows the
    overlay merged mount. Returns (policy, cleanup); cleanup removes the overlay
    upper/work/merged dirs."""
    wt = base / "worktree"
    wt.mkdir(exist_ok=True)
    base_pol = _active_policy(wt, home=home, secret_dirs=secret_dirs)
    layer, cleanup = make_home_layer(
        str(home),
        upper=base / "ov-upper", work=base / "ov-work", merged=base / "ov-merged",
        use_overlay=True)
    pol = dataclasses.replace(
        base_pol, home=str(home), writable_caches=writable_caches, home_layer=layer)
    return pol, cleanup


def _fallback_home(root: Path, home: Path, *,
                   secret_dirs: tuple[str, ...] = ()) -> SandboxPolicy:
    """Active policy with a rw-fallback HomeLayer (no overlay) over `home` — a
    plain --bind home home (writes persist to the REAL home). The fallback writes
    THROUGH to the real home by design, so a /tmp-based root is fine here (the /tmp
    shadow re-binds the same host home dir the fallback already targets)."""
    base = _active_policy(root, home=home, secret_dirs=secret_dirs)
    layer, _ = make_home_layer(
        str(home), upper=root / "u", work=root / "w", merged=root / "m",
        use_overlay=False)
    return dataclasses.replace(base, home=str(home), home_layer=layer)


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
    (root / ".gitignore").write_text(".imp/\n")
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
    policy with setenv that explicitly excludes AWS_* (matching what _build_setenv
    produces — this is the same allowlist resolve_policy bakes via _build_setenv).
    """
    root = _make_root()
    old = os.environ.get("AWS_SECRET_ACCESS_KEY")
    os.environ["AWS_SECRET_ACCESS_KEY"] = "supersecret"
    try:
        # Hand-built policy: setenv does NOT include AWS_SECRET_ACCESS_KEY.
        # This directly tests the bwrap --clearenv + --setenv mechanism.
        # (resolve_policy produces the same setenv via _build_setenv, which only
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
    """build_sandbox_argv with network=False must include --unshare-net (spec B.2 step 15)."""
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
        setenv=(("PATH", "/usr/bin:/bin"),),
        network=False,  # <-- network isolation
        note="",
    )
    argv = build_sandbox_argv("curl example.com", cwd="/tmp/fake-wt", policy=pol,
                              engine_env={})
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
    guard fired inside the bwrap --clearenv jail.
    """
    import dataclasses

    from imp.engine.gates import run_script
    from imp.engine.types import Pause, SourceLoc, Step

    root = _make_root("imp-test-title-")
    try:
        pol = _active_policy(root)
        pol = dataclasses.replace(pol, setenv=(*pol.setenv, ("IMP_QUEST_TITLE", "")))
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
      - git config --worktree core.hooksPath /tmp/evil is BLOCKED (config.worktree remasked RO).
      - Writing a SIBLING worktree's config.worktree is BLOCKED (sibling remasked by
        resolve_policy).
      - Writing .git/info/attributes succeeds but the filter DRIVER config write is BLOCKED.
    """
    control = Path(tempfile.mkdtemp(prefix="imp-test-gitesc-"))
    try:
        # --- repo setup ---
        _init_repo(control)
        # Create a sibling linked worktree BEFORE resolving policy so the sibling's
        # config.worktree appears in git_ro_remask (spec M1-r5).
        wt.create_worktree(control, "sib", "main")
        # git only creates config.worktree when 'git config --worktree' writes to it.
        # We create it explicitly so resolve_policy includes it in git_ro_remask
        # (the policy scans for existing config.worktree files).
        sib_config_wt = control / ".git" / "worktrees" / "sib" / "config.worktree"
        sib_config_wt.write_text("[core]\n\textensions = worktreeConfig\n")
        wt_path = wt.create_worktree(control, "esc1", "main")
        # control/.imp/tmp/esc1 is created by create_worktree (B4).

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
        #    esc1's OWN config.worktree does not exist at resolve-time (git only
        #    creates it on first --worktree write), so it is not in git_ro_remask.
        #    The escape is still blocked transitively: `git config --worktree`
        #    requires extensions.worktreeConfig in .git/config first, and .git/config
        #    is remasked RO — so git refuses (rc=128, "worktreeConfig not enabled")
        #    and never reaches a writable hooksPath. (Any EXISTING config.worktree,
        #    incl. siblings, IS remasked RO — see vector 5.)
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
        #    resolve_policy scans .git/worktrees/* and adds every existing
        #    config.worktree (including siblings) to git_ro_remask (spec M1-r5).
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
        argv = build_sandbox_argv(
            "python3 -c 'import imp; print(imp.__file__)'",
            cwd=str(root), policy=pol,
            engine_env={"PYTHONPATH": str(root / "src")})
        result = subprocess.run(argv, capture_output=True, text=True)
        assert result.returncode == 0, result.stderr
        assert str(worktree_imp) in result.stdout
    finally:
        shutil.rmtree(root, ignore_errors=True)


# ---------------------------------------------------------------------------
# PATH resolution (spec §7 / AC 12): the console-script `imp` on PATH runs the
# worktree's code, because the injected PYTHONPATH shadows imp.cli.main even
# though PATH itself is never modified by the sandbox.
# ---------------------------------------------------------------------------

def test_sandboxed_imp_console_script_resolves_worktree_code() -> None:
    """A bash `imp` inside a sandboxed step executes the worktree's imp.cli.main."""
    if shutil.which("imp") is None:
        pytest.skip("imp console script not on PATH")
    sentinel = "WORKTREE-IMP-SENTINEL-dafu54"
    root = _make_root()
    try:
        cli = root / "src" / "imp" / "cli"
        cli.mkdir(parents=True)
        (root / "src" / "imp" / "__init__.py").write_text("")
        (cli / "__init__.py").write_text("")
        (cli / "main.py").write_text(
            f'def main():\n    print("{sentinel}")\n    return 0\n')
        pol = _active_policy(root)

        argv_shadow = build_sandbox_argv(
            "imp --help", cwd=str(root), policy=pol,
            engine_env={"PYTHONPATH": str(root / "src")})
        shadowed = subprocess.run(argv_shadow, capture_output=True, text=True)

        argv_bare = build_sandbox_argv(
            "imp --help", cwd=str(root), policy=pol, engine_env={})
        bare = subprocess.run(argv_bare, capture_output=True, text=True)

        assert sentinel in shadowed.stdout, (
            f"sandboxed `imp` ignored the PYTHONPATH shadow — it did not run the "
            f"worktree stub (rc={shadowed.returncode}) "
            f"stdout={shadowed.stdout!r} stderr={shadowed.stderr!r}")
        assert bare.returncode == 0 and "usage" in bare.stdout.lower(), (
            f"negative control did not actually run the real imp CLI "
            f"(rc={bare.returncode}) — the `sentinel not in` check would pass "
            f"vacuously; stdout={bare.stdout!r} stderr={bare.stderr!r}")
        assert sentinel not in bare.stdout, (
            f"negative control failed: sentinel leaked with no shadow "
            f"stdout={bare.stdout!r}")
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
    guard is active (Part 1). Used for the change worktree and, with a different
    `worktree`, for the spike worktree."""
    pol = resolve_policy(
        worktree=worktree, control_root=control, quest_id="esc1",
        cfg=SandboxConfig(home=control / "home", uid=os.getuid()),
        engine_env_result={"IMP_PROJECT_ROOT": str(worktree)},
        environ=os.environ, available=True,
    )
    ctx = ToolContext(
        project_root=worktree, events_path=worktree / "events.jsonl")
    ctx.quest_context = QuestContext(
        title="Ref Test", project_root=worktree,
        workflow_dir=worktree / ".imp" / "wf")
    ctx.base_sandbox_policy = pol
    return ctx, pol


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
        # (they are shell-assignment prefixes, independent of the --clearenv jail).
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
def test_overlay_home_write_persists_across_bash_calls() -> None:
    # AC 2: a file written to $HOME in one bash call is present in a later bash call
    # within the same invocation (both calls mount the same on-disk upper). Because
    # home is NOT shadowed by a later bind, the write lands in the overlay upper —
    # so this proves shared-upper persistence, NOT a real-home leak: the falsifier
    # below asserts the real home never sees the file.
    root = _make_overlay_root()
    try:
        home = root / "home"
        home.mkdir()
        pol, cleanup = _overlay_home(root, home)
        try:
            ctx = _ctx(Path(pol.worktree), pol)
            r1 = _bash(ctx, 'echo PERSISTED > "$HOME/across.txt" && echo wrote')
            assert r1["returncode"] == 0 and "wrote" in r1["stdout"]
            r2 = _bash(ctx, 'cat "$HOME/across.txt"')
            assert r2["returncode"] == 0, f"read-back failed: {r2}"
            assert "PERSISTED" in r2["stdout"], (
                f"write did not persist across bash calls: {r2['stdout']!r}")
            # falsifier: persistence is via the shared overlay upper, not a leak —
            # the isolated real home never sees the file.
            assert not (home / "across.txt").exists()
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

        quest_dir = control / ".imp" / "quests" / "q1"
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

