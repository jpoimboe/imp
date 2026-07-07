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
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

import anyio
import pytest

import imp.agents.tools.bash  # noqa: F401 — registers the tool
from imp.agents.runner import ChangeContext
from imp.agents.tools import get_registry
from imp.agents.tools.context import ToolContext
from imp.engine import worktree as wt
from imp.engine.sandbox import (
    SandboxConfig,
    SandboxPolicy,
    build_sandbox_argv,
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
            ("PATH", os.environ.get("PATH", "/usr/bin:/bin")),
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
    ctx.change_context = ChangeContext(
        title="Test",
        project_root=worktree,
        workflow_dir=worktree / ".imp" / "wf",
    )
    ctx.sandbox_policy = policy
    return ctx


def _bash(ctx: ToolContext, cmd: str) -> dict[str, object]:
    """Run cmd under ctx's sandbox policy; return parsed JSON {stdout, stderr, returncode}."""
    async def _run() -> str:
        return await get_registry()["bash"].async_invoke(ctx, {"command": cmd})
    return json.loads(anyio.run(_run))


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
    """The sandbox env allowlist (spec B.4) only passes PATH/HOME/LANG/LC_*/IMP_*.

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
        # allows PATH/HOME/USER/LOGNAME/LANG/TERM/TZ/LC_*/IMP_* + extra_env.)
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
    """Regression: name-on-intent freezes the policy with IMP_CHANGE_TITLE="" at run
    start; after rename_change the gate's fresh env_extra carries the real title.
    Reproduces the dogfood failure where a `${IMP_CHANGE_*:?not set by engine}`
    guard fired inside the bwrap --clearenv jail.
    """
    import dataclasses

    from imp.engine.gates import run_script
    from imp.engine.types import Pause, SourceLoc, Step

    root = _make_root("imp-test-title-")
    try:
        pol = _active_policy(root)
        pol = dataclasses.replace(pol, setenv=(*pol.setenv, ("IMP_CHANGE_TITLE", "")))
        step = Step(
            name="g", phase="p", kind="script", rules=(), on_max_retries=Pause(),
            max_loops=1, source=SourceLoc(file=Path(__file__), line=1),
            command=': "${IMP_CHANGE_TITLE:?IMP_CHANGE_TITLE not set by engine}"; '
                    'printf %s "$IMP_CHANGE_TITLE"',
            timeout_seconds=30,
        )
        result = run_script(step, cwd=root, policy=pol,
                            env_extra={"IMP_CHANGE_TITLE": "Live mode log polish"})
        assert result["exit"] == 0, f"gate exited {result['exit']}: {result}"
        assert result["stdout"] == "Live mode log polish"
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_bash_tool_uses_live_change_title_not_stale_snapshot() -> None:
    """Regression: the bash tool must read IMP_CHANGE_TITLE from the LIVE Change, not
    the frozen change_context snapshot that rename_change never updates. Without the
    fix the agent's own bash sees the empty name-on-intent title inside the sandbox.
    """
    import types as _t

    root = _make_root("imp-test-bash-title-")
    try:
        pol = _active_policy(root)
        ctx = _ctx(root, pol)
        # change_context = the pre-rename snapshot (empty title); ctx.change = the
        # live, renamed Change. The bash tool must prefer the live one.
        ctx.change_context = ChangeContext(
            id="2026-06-10-x", title="",
            project_root=root, workflow_dir=root / ".imp" / "wf")
        ctx.change = _t.SimpleNamespace(
            id="2026-06-10-x", handle="", title="renamed")
        out = _bash(ctx, 'printf %s "$IMP_CHANGE_TITLE"')
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
            change_id="esc1",
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
        ctx.change_context = ChangeContext(
            title="Escape Test",
            project_root=wt_path,
            workflow_dir=wt_path / ".imp" / "wf",
        )
        ctx.sandbox_policy = pol

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
        worktree=worktree, control_root=control, change_id="esc1",
        cfg=SandboxConfig(home=control / "home", uid=os.getuid()),
        engine_env_result={"IMP_PROJECT_ROOT": str(worktree)},
        environ=os.environ, available=True,
    )
    ctx = ToolContext(
        project_root=worktree, events_path=worktree / "events.jsonl")
    ctx.change_context = ChangeContext(
        title="Ref Test", project_root=worktree,
        workflow_dir=worktree / ".imp" / "wf")
    ctx.sandbox_policy = pol
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

        # Change branch: two commits then an autosquash rebase onto main. The
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
        # at the spike worktree (mirrors _build_spike_policy inheriting the guard).
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
