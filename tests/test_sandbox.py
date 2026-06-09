"""Always-on sandbox tests — no bwrap needed (pure argv + policy resolution)."""
from __future__ import annotations

import pytest

from imp.engine.sandbox import (
    SandboxConfig,
    SandboxPolicy,
    SandboxUnavailable,
    build_sandbox_argv,
    resolve_policy,
    sandbox_available,
)


def _policy(**over) -> SandboxPolicy:
    base = dict(
        active=True,
        worktree="/ctl/.imp/worktrees/abc",
        git_common_dir="/ctl/.git",
        git_ro_remask=("/ctl/.git/hooks", "/ctl/.git/config",
                       "/ctl/.git/worktrees/abc/config.worktree",
                       "/ctl/.git/worktrees/sib/config.worktree"),
        writable_caches=("/home/u/.cache", "/home/u/.cargo"),
        extra_readable=(),
        extra_writable=(),
        tmp_host_dir="/ctl/.imp/tmp/abc",
        secret_dirs=("/home/u/.ssh", "/run/user/1000"),
        secret_files=("/home/u/.netrc",),
        setenv=(("PATH", "/usr/bin"), ("HOME", "/home/u"),
                ("IMP_PROJECT_ROOT", "/ctl/.imp/worktrees/abc"),
                ("TMPDIR", "/tmp")),
        network=True,
        note="",
    )
    base.update(over)
    return SandboxPolicy(**base)


def test_inactive_policy_returns_bare_argv():
    argv = build_sandbox_argv("echo hi", cwd="/ctl/.imp/worktrees/abc",
                              policy=_policy(active=False))
    assert argv == ["sh", "-c", "echo hi"]


def test_active_policy_wraps_with_bwrap():
    argv = build_sandbox_argv("echo hi", cwd="/ctl/.imp/worktrees/abc",
                              policy=_policy())
    assert argv[0] == "bwrap"
    assert argv[-3:] == ["sh", "-c", "echo hi"]
    assert argv[1:4] == ["--ro-bind", "/", "/"]   # ro-floor first


def test_worktree_and_git_are_rw_bound_at_real_path():
    argv = " ".join(build_sandbox_argv("x", cwd="/wt", policy=_policy()))
    assert "--bind /ctl/.imp/worktrees/abc /ctl/.imp/worktrees/abc" in argv
    assert "--bind /ctl/.git /ctl/.git" in argv


def test_git_escape_vectors_are_remasked_ro_including_siblings():
    argv = " ".join(build_sandbox_argv("x", cwd="/wt", policy=_policy()))
    assert "--ro-bind /ctl/.git/hooks /ctl/.git/hooks" in argv
    assert "--ro-bind /ctl/.git/config /ctl/.git/config" in argv
    assert "--ro-bind /ctl/.git/worktrees/sib/config.worktree" in argv  # sibling (M1)


def test_secret_masks_emitted_after_every_bind_even_overlapping_extra_writable():
    # L1-r5: a mask placed BEFORE an overlapping extra_writable would be clobbered.
    p = _policy(extra_writable=("/home/u",), secret_dirs=("/home/u/.ssh",))
    flat = build_sandbox_argv("x", cwd="/wt", policy=p)
    i_writable = flat.index("/home/u")                      # the extra_writable bind dest
    i_tmpfs = flat.index("--tmpfs")
    assert i_tmpfs > i_writable, "secret --tmpfs must come AFTER extra_writable bind"


def test_tmp_is_bound_persistent_not_tmpfs():
    argv = " ".join(build_sandbox_argv("x", cwd="/wt", policy=_policy()))
    assert "--bind /ctl/.imp/tmp/abc /tmp" in argv


def test_clearenv_and_setenv_allowlist():
    argv = " ".join(build_sandbox_argv("x", cwd="/wt", policy=_policy()))
    assert "--clearenv" in argv
    assert "--setenv IMP_PROJECT_ROOT /ctl/.imp/worktrees/abc" in argv
    assert "--setenv TMPDIR /tmp" in argv


def test_no_unshare_pid_and_no_die_with_parent():
    argv = build_sandbox_argv("x", cwd="/wt", policy=_policy())
    assert "--unshare-pid" not in argv      # B.9.1
    assert "--die-with-parent" not in argv  # B.9.2 (H-A)
    assert "--new-session" not in argv      # B.9.3 (F-8)
    assert "--unshare-user" in argv


def test_network_off_adds_unshare_net():
    on = build_sandbox_argv("x", cwd="/wt", policy=_policy(network=True))
    off = build_sandbox_argv("x", cwd="/wt", policy=_policy(network=False))
    assert "--unshare-net" not in on
    assert "--unshare-net" in off


def test_chdir_is_cwd():
    argv = " ".join(build_sandbox_argv("x", cwd="/ctl/.imp/worktrees/abc/sub",
                                       policy=_policy()))
    assert "--chdir /ctl/.imp/worktrees/abc/sub" in argv


def _layout(tmp_path):
    control = tmp_path / "ctl"
    wt = control / ".imp" / "worktrees" / "abc"
    wt.mkdir(parents=True)
    (control / ".git" / "hooks").mkdir(parents=True)
    (control / ".git" / "config").write_text("")
    wdir = control / ".git" / "worktrees" / "abc"
    wdir.mkdir(parents=True)
    (wdir / "config.worktree").write_text("")
    (control / ".imp" / "tmp" / "abc").mkdir(parents=True)
    return control, wt


def test_resolve_policy_active_when_enabled_and_available(tmp_path):
    control, wt = _layout(tmp_path)
    pol = resolve_policy(
        worktree=wt, control_root=control, change_id="abc",
        cfg=SandboxConfig(),  # defaults: enabled=True, network=True
        engine_env_result={"IMP_PROJECT_ROOT": str(wt)},
        environ={"PATH": "/usr/bin", "HOME": str(tmp_path)},
        available=True,
    )
    assert pol.active is True
    assert pol.worktree == str(wt)
    assert pol.git_common_dir == str(control / ".git")
    assert ("IMP_PROJECT_ROOT", str(wt)) in pol.setenv
    assert ("TMPDIR", "/tmp") in pol.setenv
    assert pol.tmp_host_dir == str(control / ".imp" / "tmp" / "abc")


def test_resolve_policy_binds_sibling_config_worktree(tmp_path):
    control, wt = _layout(tmp_path)
    sib = control / ".git" / "worktrees" / "sib"
    sib.mkdir(parents=True)
    (sib / "config.worktree").write_text("")
    pol = resolve_policy(worktree=wt, control_root=control, change_id="abc",
                         cfg=SandboxConfig(), engine_env_result={},
                         environ={}, available=True)
    assert str(sib / "config.worktree") in pol.git_ro_remask


def test_resolve_policy_only_existing_caches_and_secrets(tmp_path):
    control, wt = _layout(tmp_path)
    home = tmp_path / "home"
    (home / ".cache").mkdir(parents=True)        # exists
    (home / ".ssh").mkdir()                       # exists
    # .cargo, .aws do NOT exist
    pol = resolve_policy(
        worktree=wt, control_root=control, change_id="abc",
        cfg=SandboxConfig(home=home), engine_env_result={},
        environ={}, available=True,
    )
    assert str(home / ".cache") in pol.writable_caches
    assert str(home / ".cargo") not in pol.writable_caches
    assert str(home / ".ssh") in pol.secret_dirs
    assert str(home / ".aws") not in pol.secret_dirs


def test_resolve_policy_masks_runtime_dir(tmp_path):
    control, wt = _layout(tmp_path)
    pol = resolve_policy(worktree=wt, control_root=control, change_id="abc",
                         cfg=SandboxConfig(uid=1000), engine_env_result={},
                         environ={}, available=True)
    assert "/run/user/1000" in pol.secret_dirs


def test_resolve_policy_env_drops_secrets_keeps_imp_and_lc(tmp_path):
    control, wt = _layout(tmp_path)
    pol = resolve_policy(
        worktree=wt, control_root=control, change_id="abc", cfg=SandboxConfig(),
        engine_env_result={"IMP_LINT_COMMAND": "make lint"},
        environ={"PATH": "/usr/bin", "AWS_SECRET_ACCESS_KEY": "x",
                 "LC_ALL": "C", "ANTHROPIC_API_KEY": "k"},
        available=True,
    )
    keys = {k for k, _ in pol.setenv}
    assert "PATH" in keys and "LC_ALL" in keys and "IMP_LINT_COMMAND" in keys
    assert "AWS_SECRET_ACCESS_KEY" not in keys
    assert "ANTHROPIC_API_KEY" not in keys


def test_resolve_policy_disabled_is_inactive(tmp_path):
    control, wt = _layout(tmp_path)
    pol = resolve_policy(worktree=wt, control_root=control, change_id="abc",
                         cfg=SandboxConfig(enabled=False), engine_env_result={},
                         environ={}, available=True)
    assert pol.active is False and pol.note == "sandbox_disabled"


def test_resolve_policy_unavailable_with_allow_runs_bare(tmp_path):
    control, wt = _layout(tmp_path)
    pol = resolve_policy(worktree=wt, control_root=control, change_id="abc",
                         cfg=SandboxConfig(allow_unsandboxed=True),
                         engine_env_result={}, environ={}, available=False)
    assert pol.active is False and pol.note == "unsandboxed_by_config"


def test_resolve_policy_unavailable_without_allow_hard_fails(tmp_path):
    control, wt = _layout(tmp_path)
    with pytest.raises(SandboxUnavailable):
        resolve_policy(worktree=wt, control_root=control, change_id="abc",
                       cfg=SandboxConfig(allow_unsandboxed=False),
                       engine_env_result={}, environ={}, available=False)


def test_sandbox_available_is_cached_bool():
    assert isinstance(sandbox_available(), bool)
