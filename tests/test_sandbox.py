"""Always-on sandbox tests — no bwrap needed (pure argv + policy resolution)."""
from __future__ import annotations

import types as _types
from pathlib import Path

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
                              policy=_policy(active=False), engine_env={})
    assert argv == ["sh", "-c", "echo hi"]


def test_active_policy_wraps_with_bwrap():
    argv = build_sandbox_argv("echo hi", cwd="/ctl/.imp/worktrees/abc",
                              policy=_policy(), engine_env={})
    assert argv[0] == "bwrap"
    assert argv[-3:] == ["sh", "-c", "echo hi"]
    assert argv[1:4] == ["--ro-bind", "/", "/"]   # ro-floor first


def test_worktree_and_git_are_rw_bound_at_real_path():
    argv = " ".join(build_sandbox_argv("x", cwd="/wt", policy=_policy(), engine_env={}))
    assert "--bind /ctl/.imp/worktrees/abc /ctl/.imp/worktrees/abc" in argv
    assert "--bind /ctl/.git /ctl/.git" in argv


def test_git_escape_vectors_are_remasked_ro_including_siblings():
    argv = " ".join(build_sandbox_argv("x", cwd="/wt", policy=_policy(), engine_env={}))
    assert "--ro-bind /ctl/.git/hooks /ctl/.git/hooks" in argv
    assert "--ro-bind /ctl/.git/config /ctl/.git/config" in argv
    assert "--ro-bind /ctl/.git/worktrees/sib/config.worktree" in argv  # sibling (M1)


def test_secret_masks_emitted_after_every_bind_even_overlapping_extra_writable():
    # L1-r5: a mask placed BEFORE an overlapping extra_writable would be clobbered.
    p = _policy(extra_writable=("/home/u",), secret_dirs=("/home/u/.ssh",))
    flat = build_sandbox_argv("x", cwd="/wt", policy=p, engine_env={})
    i_writable = flat.index("/home/u")                      # the extra_writable bind dest
    i_tmpfs = flat.index("--tmpfs")
    assert i_tmpfs > i_writable, "secret --tmpfs must come AFTER extra_writable bind"


def test_real_tmp_is_bound_rw_early():
    argv = build_sandbox_argv("x", cwd="/wt", policy=_policy(), engine_env={})
    flat = " ".join(argv)
    # Real /tmp is bound rw (identity bind).
    assert "--bind /tmp /tmp" in flat
    # Ordering: /tmp bind must appear right after the ro-floor and BEFORE
    # the worktree bind (bwrap last-wins; /tmp early means worktree/git/remask
    # still win on their paths even if a repo lives under /tmp).
    i_ro_floor = argv.index("/")          # first "/" after --ro-bind is the src
    i_tmp_bind = argv.index("/tmp")       # first "/tmp" after --bind is the src
    i_wt_bind = argv.index("/ctl/.imp/worktrees/abc")  # worktree src
    # The load-bearing invariant: /tmp must be bound BEFORE the git RO-remask, so a
    # repo whose .git lives under /tmp still has hooks/config remasked RO (the late
    # remask wins over the early /tmp bind). Assert directly against the remask, not
    # just transitively via the worktree bind.
    i_first_remask = argv.index("/ctl/.git/hooks")
    assert i_tmp_bind > i_ro_floor, "/tmp bind must come after ro-floor"
    assert i_tmp_bind < i_wt_bind, "/tmp bind must come BEFORE worktree bind"
    assert i_tmp_bind < i_first_remask, "/tmp bind must come BEFORE the git RO-remask"


def test_clearenv_and_setenv_allowlist():
    argv = " ".join(build_sandbox_argv("x", cwd="/wt", policy=_policy(), engine_env={}))
    assert "--clearenv" in argv
    assert "--setenv IMP_PROJECT_ROOT /ctl/.imp/worktrees/abc" in argv
    assert "--setenv TMPDIR /tmp" in argv


def test_live_engine_env_supersedes_frozen_imp_vars():
    # Regression (name-on-intent + sandbox): a Change is created with an empty
    # title, so the policy is frozen with IMP_CHANGE_TITLE="" at run start. After
    # rename_change the live per-spawn engine_env carries the real title;
    # build_sandbox_argv sources IMP_* from it authoritatively, so a title-dependent
    # gate never sees the frozen "".
    pol = _policy(setenv=(("PATH", "/usr/bin"),
                          ("IMP_CHANGE_TITLE", ""),
                          ("TMPDIR", "/tmp")))
    argv = build_sandbox_argv(
        "x", cwd="/wt", policy=pol,
        engine_env={"IMP_CHANGE_TITLE": "Live mode log polish",
                    "PYTHONPATH": "/wt/src"})
    assert argv.count("IMP_CHANGE_TITLE") == 1       # not duplicated
    i = argv.index("IMP_CHANGE_TITLE")
    assert argv[i + 1] == "Live mode log polish"   # fresh value wins
    assert "PYTHONPATH" not in argv                 # non-IMP_ key stays out (prefix allowlist)


def test_new_imp_var_flows_through_without_code_change():
    # Future-proofing: the IMP_ prefix is the allowlist, so a brand-new engine var
    # reaches the sandbox with no edit to build_sandbox_argv.
    argv = build_sandbox_argv(
        "x", cwd="/wt", policy=_policy(),
        engine_env={"IMP_FUTURE_VAR": "v", "SOME_OTHER": "no"})
    assert "--setenv IMP_FUTURE_VAR v" in " ".join(argv)
    assert "SOME_OTHER" not in argv


def test_no_unshare_pid_and_no_die_with_parent():
    argv = build_sandbox_argv("x", cwd="/wt", policy=_policy(), engine_env={})
    assert "--unshare-pid" not in argv      # B.9.1
    assert "--die-with-parent" not in argv  # B.9.2 (H-A)
    assert "--new-session" not in argv      # B.9.3 (F-8)
    assert "--unshare-user" in argv


def test_network_off_adds_unshare_net():
    on = build_sandbox_argv("x", cwd="/wt", policy=_policy(network=True), engine_env={})
    off = build_sandbox_argv("x", cwd="/wt", policy=_policy(network=False), engine_env={})
    assert "--unshare-net" not in on
    assert "--unshare-net" in off


def test_chdir_is_cwd():
    argv = " ".join(build_sandbox_argv("x", cwd="/ctl/.imp/worktrees/abc/sub",
                                       policy=_policy(), engine_env={}))
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
        engine_env_result={"IMP_VERIFY_COMMAND": "make check"},
        environ={"PATH": "/usr/bin", "AWS_SECRET_ACCESS_KEY": "x",
                 "LC_ALL": "C", "ANTHROPIC_API_KEY": "k"},
        available=True,
    )
    keys = {k for k, _ in pol.setenv}
    assert "PATH" in keys and "LC_ALL" in keys and "IMP_VERIFY_COMMAND" in keys
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


def test_resolve_policy_secret_paths_classified_dir_and_file(tmp_path):
    control, wt = _layout(tmp_path)
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    (home / ".mykeys").mkdir()           # dir
    (home / ".mytoken").write_text("x")  # file
    pol = resolve_policy(
        worktree=wt, control_root=control, change_id="abc",
        cfg=SandboxConfig(home=home, secret_paths=(".mykeys", ".mytoken")),
        engine_env_result={}, environ={}, available=True,
    )
    assert str(home / ".mykeys") in pol.secret_dirs
    assert str(home / ".mytoken") in pol.secret_files


def test_resolve_policy_writable_caches_override_replaces_defaults(tmp_path):
    control, wt = _layout(tmp_path)
    home = tmp_path / "home"
    (home / ".customcache").mkdir(parents=True)
    (home / ".cache").mkdir(parents=True)
    pol = resolve_policy(
        worktree=wt, control_root=control, change_id="abc",
        cfg=SandboxConfig(home=home, writable_caches=(".customcache",)),
        engine_env_result={}, environ={}, available=True,
    )
    assert str(home / ".customcache") in pol.writable_caches
    assert str(home / ".cache") not in pol.writable_caches


def test_resolve_policy_extra_readable_writable_plumbed(tmp_path):
    control, wt = _layout(tmp_path)
    pol = resolve_policy(
        worktree=wt, control_root=control, change_id="abc",
        cfg=SandboxConfig(extra_readable=("/opt/ro",), extra_writable=("/opt/rw",)),
        engine_env_result={}, environ={}, available=True,
    )
    assert "/opt/ro" in pol.extra_readable
    assert "/opt/rw" in pol.extra_writable


def test_build_setenv_dedups_overlapping_extra_env(tmp_path):
    control, wt = _layout(tmp_path)
    pol = resolve_policy(
        worktree=wt, control_root=control, change_id="abc",
        cfg=SandboxConfig(extra_env=("TMPDIR",)),
        engine_env_result={},
        environ={"PATH": "/usr/bin", "TMPDIR": "/var/tmp"},
        available=True,
    )
    # Forced /tmp override must win.
    assert ("TMPDIR", "/tmp") in pol.setenv
    # No key appears more than once.
    keys = [k for k, _ in pol.setenv]
    assert len(keys) == len(set(keys))


def test_resolve_policy_uid_zero_masks_run_user_0(tmp_path):
    control, wt = _layout(tmp_path)
    pol = resolve_policy(
        worktree=wt, control_root=control, change_id="abc",
        cfg=SandboxConfig(uid=0),
        engine_env_result={}, environ={}, available=True,
    )
    assert "/run/user/0" in pol.secret_dirs


# ── _build_sandbox_policy unit tests (B.7) ───────────────────────────────────


def _fake_change(tmp_path):
    control = tmp_path / "ctl"
    wt = control / ".imp" / "worktrees" / "abc"
    wt.mkdir(parents=True, exist_ok=True)
    (control / ".git").mkdir(parents=True, exist_ok=True)
    return _types.SimpleNamespace(
        project_root=wt, control_root=control, id="abc",
        handle="abc-slug", title="Abc Title",
        change_dir=wt / ".imp" / "change")


def _fake_config(**over):
    base = dict(sandbox={}, verify_command=None, verify_timeout=None)
    base.update(over)
    return _types.SimpleNamespace(**base)


class _CaptureLog:
    """Minimal EventLog stub: records every log.append() call as an event dict."""
    def __init__(self):
        self.events = []

    def append(self, *, agent_id, type, payload):
        record = {"type": type, "payload": payload, "agent_id": agent_id,
                  "id": str(len(self.events))}
        self.events.append(record)
        return record

    # Protocol methods that _emit does NOT call but EventLog declares; stubs only.
    def read(self):
        return iter(self.events)

    def append_step_context(self, **_kw): ...
    def append_user_message(self, **_kw): ...
    def append_llm_response(self, **_kw): ...
    def append_tool_call(self, **_kw): ...
    def append_tool_result(self, **_kw): ...
    def update_last_llm_response_usage(self, **_kw): ...


@pytest.mark.real_sandbox_policy
def test_build_sandbox_policy_hard_fails_when_unavailable(tmp_path, monkeypatch):
    """enabled + unavailable + allow_unsandboxed unset → the run must NOT silently
    proceed unsandboxed: _build_sandbox_policy re-raises SandboxUnavailable (B.7)."""
    import imp.engine.executor as ex
    monkeypatch.setattr(ex, "sandbox_available", lambda: False)
    # Clear the lru_cache so the monkeypatched function is actually used.
    from imp.engine import sandbox as _sb
    _sb.sandbox_available.cache_clear()

    change = _fake_change(tmp_path)
    config = _fake_config(sandbox={})      # enabled defaults True, allow_unsandboxed False
    log = _CaptureLog()
    with pytest.raises(SandboxUnavailable):
        ex._build_sandbox_policy(change, config, log)
    assert any(e["type"] == "sandbox_unavailable" for e in log.events)


@pytest.mark.real_sandbox_policy
def test_build_sandbox_policy_bare_when_allow_unsandboxed(tmp_path, monkeypatch):
    import imp.engine.executor as ex
    monkeypatch.setattr(ex, "sandbox_available", lambda: False)
    from imp.engine import sandbox as _sb
    _sb.sandbox_available.cache_clear()

    change = _fake_change(tmp_path)
    config = _fake_config(sandbox={"allow_unsandboxed": True})
    log = _CaptureLog()
    pol = ex._build_sandbox_policy(change, config, log)
    assert pol.active is False and pol.note == "unsandboxed_by_config"
    # The inactive posture must be auditable: a sandbox_inactive event is emitted
    # carrying the note (so an operator can see the run went unsandboxed).
    inactive = [e for e in log.events if e["type"] == "sandbox_inactive"]
    assert inactive and inactive[0]["payload"]["reason"] == "unsandboxed_by_config"


@pytest.mark.real_sandbox_policy
def test_build_sandbox_policy_active_path_builds_policy_silently(tmp_path, monkeypatch):
    """The active (happy) path: available+enabled → active policy, no posture
    event (active is the silent common case), and the engine_env overlay reaches
    setenv as IMP_PROJECT_ROOT."""
    import imp.engine.executor as ex
    monkeypatch.setattr(ex, "sandbox_available", lambda: True)
    from imp.engine import sandbox as _sb
    _sb.sandbox_available.cache_clear()

    change = _fake_change(tmp_path)
    config = _fake_config(sandbox={})
    log = _CaptureLog()
    pol = ex._build_sandbox_policy(change, config, log)
    assert pol.active is True and pol.note == ""
    assert pol.worktree == str(change.project_root)
    assert ("IMP_PROJECT_ROOT", str(change.project_root)) in pol.setenv
    # active is the silent common case — no posture event is emitted.
    assert not any(e["type"] in ("sandbox_inactive", "sandbox_unavailable")
                   for e in log.events)


def _mirror_policy() -> SandboxPolicy:
    # A synthetic policy that exercises every bind class, including a remask
    # under git_common_dir, a remask OUTSIDE all writable roots, and an
    # extra_writable that overlaps a remask (to pin positional last-wins).
    return _policy(
        worktree="/wt",
        git_common_dir="/ctl/.git",
        git_ro_remask=("/ctl/.git/hooks", "/ctl/.git/config", "/elsewhere/ro"),
        writable_caches=("/home/u/.cache",),
        extra_readable=("/ro_extra",),
        extra_writable=("/rw_extra",),
        secret_dirs=("/home/u/.ssh", "/run/user/1000"),
        secret_files=("/home/u/.netrc",),
    )


def test_allows_write_writable_roots():
    p = _mirror_policy()
    assert p.allows_write(Path("/wt/file")) is True
    assert p.allows_write(Path("/ctl/.git/objects/x")) is True   # git_common_dir
    assert p.allows_write(Path("/tmp/scratch.txt")) is True
    assert p.allows_write(Path("/home/u/.cache/x")) is True      # writable cache
    assert p.allows_write(Path("/rw_extra/x")) is True           # extra_writable


def test_allows_write_uncovered_and_masked_denied():
    p = _mirror_policy()
    assert p.allows_write(Path("/usr/lib/x")) is False           # no covering bind
    assert p.allows_write(Path("/home/u/.ssh/id_rsa")) is False  # secret dir mask
    assert p.allows_write(Path("/home/u/.netrc")) is False       # secret file mask


def test_allows_write_ro_remask_denied_both_positions():
    p = _mirror_policy()
    # Remask UNDER git_common_dir: the step-5 ro-bind wins over the step-4 git
    # writable bind — a mirror that only subtracts the remask from git_common_dir
    # would (wrongly) still pass this if it dropped the outside case below.
    assert p.allows_write(Path("/ctl/.git/hooks/pre-commit")) is False
    # Remask OUTSIDE all writable roots: covered only by the remask, denied.
    assert p.allows_write(Path("/elsewhere/ro/x")) is False


def test_allows_write_extra_readable_denied():
    p = _mirror_policy()
    # The symmetric read-only-overlay case (extra_readable, step 7).
    assert p.allows_write(Path("/ro_extra/x")) is False


def test_allows_write_later_writable_bind_reexposes_remask():
    # Positional last-wins, NOT "ro overlay is globally last-wins": an
    # extra_writable (step 8) covering a remask path (step 5) re-exposes it,
    # exactly as bwrap would. A globally-last-wins mirror fails this. Built via
    # _policy (SandboxPolicy is a frozen slots dataclass — construct, don't mutate).
    p = _policy(
        worktree="/wt", git_common_dir="/ctl/.git",
        git_ro_remask=("/ctl/.git/hooks",),
        extra_writable=("/ctl/.git/hooks",),
        writable_caches=(), extra_readable=(),
        secret_dirs=(), secret_files=(),
    )
    assert p.allows_write(Path("/ctl/.git/hooks/pre-commit")) is True


def test_allows_read_masks_secrets_allows_rest():
    p = _mirror_policy()
    assert p.allows_read(Path("/home/u/.ssh/id_rsa")) is False   # secret dir
    assert p.allows_read(Path("/home/u/.netrc")) is False        # secret file (exact)
    assert p.allows_read(Path("/usr/lib/x")) is True             # ordinary host path
    assert p.allows_read(Path("/wt/file")) is True               # worktree path


def _parse_argv_binds(argv: list[str]) -> list[tuple[str, str]]:
    """Recover the (path, kind) path-covering binds from a real build_sandbox_argv
    emission — the inverse of build_sandbox_argv's kind→flag map. Scans the bind
    block between the step-1 ro-floor (--ro-bind / /) and step-11 (--proc),
    classifying each flag back to a bind_plan kind."""
    i = argv.index("--ro-bind") + 3        # skip "bwrap" + the "--ro-bind / /" floor
    stop = argv.index("--proc")
    out: list[tuple[str, str]] = []
    while i < stop:
        flag = argv[i]
        if flag == "--bind":
            out.append((argv[i + 1], "rw"))
            i += 3
        elif flag == "--tmpfs":
            out.append((argv[i + 1], "mask_dir"))
            i += 2
        elif flag == "--ro-bind" and argv[i + 1] == "/dev/null":
            out.append((argv[i + 2], "mask_file"))
            i += 3
        elif flag == "--ro-bind":
            out.append((argv[i + 1], "ro"))
            i += 3
        else:
            raise AssertionError(f"unexpected token {flag!r} in bind block")
    return out


def test_argv_binds_match_bind_plan():
    # Drift guard: the two consumers of the bind order — build_sandbox_argv's argv
    # and policy.bind_plan() (which allows_write evaluates) — must agree exactly.
    # Parsing the emitted argv back to (path, kind) and comparing to bind_plan()
    # means a new bind added to the plan MUST surface in argv (and vice-versa), or
    # this fails. _mirror_policy exercises every kind: rw / ro / mask_dir / mask_file.
    p = _mirror_policy()
    argv = build_sandbox_argv("x", cwd="/wt", policy=p, engine_env={})
    assert _parse_argv_binds(argv) == p.bind_plan()


def test_resolve_policy_binds_change_home_rw(tmp_path):
    control, wt = _layout(tmp_path)
    home = control / ".imp" / "changes" / "abc"
    pol = resolve_policy(
        worktree=wt, control_root=control, change_id="abc",
        cfg=SandboxConfig(), engine_env_result={}, environ={},
        available=True, change_dir=home,
    )
    assert pol.change_home == str(home)
    # An artifact write under the home is permitted by the in-process mirror...
    assert pol.allows_write(home / "artifacts" / "spec" / "x.md")
    # ...and the bwrap argv carries the matching rw bind.
    argv = " ".join(build_sandbox_argv("x", cwd=str(wt), policy=pol, engine_env={}))
    assert f"--bind {home} {home}" in argv


def test_readonly_overlay_denies_write_inside_allows_sibling():
    p = _policy(
        worktree="/wt", git_common_dir="/ctl/.git",
        git_ro_remask=(), writable_caches=(), extra_readable=(),
        extra_writable=(), secret_dirs=(), secret_files=(),
        readonly_overlays=("/wt/src",),
    )
    assert p.allows_write(Path("/wt/src/app.py")) is False     # inside the overlay
    assert p.allows_write(Path("/wt/docs/readme.md")) is True   # sibling stays rw


def test_readonly_overlay_whole_worktree_keeps_change_home_writable():
    p = _policy(
        worktree="/ctl/.imp/worktrees/abc",
        change_home="/ctl/.imp/changes/abc",
        git_common_dir="/ctl/.git",
        git_ro_remask=(), writable_caches=(), extra_readable=(),
        extra_writable=(), secret_dirs=(), secret_files=(),
        readonly_overlays=("/ctl/.imp/worktrees/abc",),
    )
    # Whole-worktree ro overlay: source is locked...
    assert p.allows_write(Path("/ctl/.imp/worktrees/abc/src/x.py")) is False
    # ...but change_home lives OUTSIDE the worktree, so it stays rw.
    assert p.allows_write(Path("/ctl/.imp/changes/abc/progress.md")) is True


def test_writable_cache_wins_over_readonly_overlay():
    p = _policy(
        worktree="/wt", git_common_dir="/ctl/.git",
        git_ro_remask=(), writable_caches=("/wt/cache",),
        extra_readable=(), extra_writable=(),
        secret_dirs=(), secret_files=(),
        readonly_overlays=("/wt",),  # whole worktree ro
    )
    assert p.allows_write(Path("/wt/src/x.py")) is False   # locked by overlay
    assert p.allows_write(Path("/wt/cache/pip")) is True   # cache punches through


def test_argv_binds_match_bind_plan_with_readonly_overlay():
    p = _policy(
        worktree="/wt", git_common_dir="/ctl/.git",
        git_ro_remask=("/ctl/.git/hooks",), writable_caches=(),
        extra_readable=(), extra_writable=(),
        secret_dirs=(), secret_files=(),
        readonly_overlays=("/wt/vendored",),
    )
    argv = build_sandbox_argv("x", cwd="/wt", policy=p, engine_env={})
    assert _parse_argv_binds(argv) == p.bind_plan()           # no drift
    assert ("/wt/vendored", "ro") in p.bind_plan()
