"""Always-on sandbox tests — no bwrap needed (pure argv + policy resolution)."""
from __future__ import annotations

import itertools
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
    # the single deliberate non-IMP_ passthrough
    assert "--setenv PYTHONPATH /wt/src" in " ".join(argv)


def test_new_imp_var_flows_through_without_code_change():
    # Future-proofing: the IMP_ prefix is the allowlist, so a brand-new engine var
    # reaches the sandbox with no edit to build_sandbox_argv.
    argv = build_sandbox_argv(
        "x", cwd="/wt", policy=_policy(),
        engine_env={"IMP_FUTURE_VAR": "v", "SOME_OTHER": "no"})
    assert "--setenv IMP_FUTURE_VAR v" in " ".join(argv)
    assert "SOME_OTHER" not in argv


def test_pythonpath_flows_through_build_sandbox_argv():
    # PYTHONPATH is the single deliberate non-IMP_ key the allowlist passes
    # through (the imp-on-imp import shadow): when the live engine_env carries it,
    # the sandboxed spawn must see it via --setenv.
    argv = build_sandbox_argv(
        "x", cwd="/wt", policy=_policy(),
        engine_env={"PYTHONPATH": "/ctl/.imp/worktrees/abc/src"})
    assert "--setenv PYTHONPATH /ctl/.imp/worktrees/abc/src" in " ".join(argv)


def test_no_pythonpath_setenv_when_engine_env_lacks_it():
    # A non-imp project's engine_env carries no PYTHONPATH, so none is emitted.
    argv = build_sandbox_argv("x", cwd="/wt", policy=_policy(), engine_env={})
    assert "PYTHONPATH" not in argv


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
        worktree=wt, control_root=control, quest_id="abc",
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
    pol = resolve_policy(worktree=wt, control_root=control, quest_id="abc",
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
        worktree=wt, control_root=control, quest_id="abc",
        cfg=SandboxConfig(home=home), engine_env_result={},
        environ={}, available=True,
    )
    assert str(home / ".cache") in pol.writable_caches
    assert str(home / ".cargo") not in pol.writable_caches
    assert str(home / ".ssh") in pol.secret_dirs
    assert str(home / ".aws") not in pol.secret_dirs


def test_resolve_policy_masks_runtime_dir(tmp_path):
    control, wt = _layout(tmp_path)
    pol = resolve_policy(worktree=wt, control_root=control, quest_id="abc",
                         cfg=SandboxConfig(uid=1000), engine_env_result={},
                         environ={}, available=True)
    assert "/run/user/1000" in pol.secret_dirs


def test_resolve_policy_env_drops_secrets_keeps_imp_and_lc(tmp_path):
    control, wt = _layout(tmp_path)
    pol = resolve_policy(
        worktree=wt, control_root=control, quest_id="abc", cfg=SandboxConfig(),
        engine_env_result={"IMP_TEST_CMD_FAST": "make check"},
        environ={"PATH": "/usr/bin", "AWS_SECRET_ACCESS_KEY": "x",
                 "LC_ALL": "C", "ANTHROPIC_API_KEY": "k"},
        available=True,
    )
    keys = {k for k, _ in pol.setenv}
    assert "PATH" in keys and "LC_ALL" in keys and "IMP_TEST_CMD_FAST" in keys
    assert "AWS_SECRET_ACCESS_KEY" not in keys
    assert "ANTHROPIC_API_KEY" not in keys


def test_resolve_policy_disabled_is_inactive(tmp_path):
    control, wt = _layout(tmp_path)
    pol = resolve_policy(worktree=wt, control_root=control, quest_id="abc",
                         cfg=SandboxConfig(enabled=False), engine_env_result={},
                         environ={}, available=True)
    assert pol.active is False and pol.note == "sandbox_disabled"


def test_resolve_policy_unavailable_with_allow_runs_bare(tmp_path):
    control, wt = _layout(tmp_path)
    pol = resolve_policy(worktree=wt, control_root=control, quest_id="abc",
                         cfg=SandboxConfig(allow_unsandboxed=True),
                         engine_env_result={}, environ={}, available=False)
    assert pol.active is False and pol.note == "unsandboxed_by_config"


def test_resolve_policy_unavailable_without_allow_hard_fails(tmp_path):
    control, wt = _layout(tmp_path)
    with pytest.raises(SandboxUnavailable):
        resolve_policy(worktree=wt, control_root=control, quest_id="abc",
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
        worktree=wt, control_root=control, quest_id="abc",
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
        worktree=wt, control_root=control, quest_id="abc",
        cfg=SandboxConfig(home=home, writable_caches=(".customcache",)),
        engine_env_result={}, environ={}, available=True,
    )
    assert str(home / ".customcache") in pol.writable_caches
    assert str(home / ".cache") not in pol.writable_caches


def test_resolve_policy_extra_readable_writable_plumbed(tmp_path):
    control, wt = _layout(tmp_path)
    pol = resolve_policy(
        worktree=wt, control_root=control, quest_id="abc",
        cfg=SandboxConfig(extra_readable=("/opt/ro",), extra_writable=("/opt/rw",)),
        engine_env_result={}, environ={}, available=True,
    )
    assert "/opt/ro" in pol.extra_readable
    assert "/opt/rw" in pol.extra_writable


def test_build_setenv_dedups_overlapping_extra_env(tmp_path):
    control, wt = _layout(tmp_path)
    pol = resolve_policy(
        worktree=wt, control_root=control, quest_id="abc",
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


def test_build_setenv_passes_pythonpath_from_engine_env(tmp_path):
    control, wt = _layout(tmp_path)
    pol = resolve_policy(
        worktree=wt, control_root=control, quest_id="abc",
        cfg=SandboxConfig(),
        engine_env_result={"IMP_PROJECT_ROOT": str(wt),
                           "PYTHONPATH": str(wt / "src")},
        environ={"PATH": "/usr/bin"},
        available=True,
    )
    assert ("PYTHONPATH", str(wt / "src")) in pol.setenv
    assert ("IMP_PROJECT_ROOT", str(wt)) in pol.setenv


def test_build_setenv_omits_pythonpath_when_engine_env_lacks_it(tmp_path):
    control, wt = _layout(tmp_path)
    pol = resolve_policy(
        worktree=wt, control_root=control, quest_id="abc",
        cfg=SandboxConfig(),
        engine_env_result={"IMP_PROJECT_ROOT": str(wt)},
        environ={"PATH": "/usr/bin"},
        available=True,
    )
    keys = {k for k, _ in pol.setenv}
    assert "PYTHONPATH" not in keys


def test_build_setenv_admits_ambient_imp_toggles(tmp_path):
    # AC 5: user-set non-engine IMP_* vars in the launching environment reach the
    # sandboxed agent, and an engine-managed var still arrives authoritatively.
    control, wt = _layout(tmp_path)
    pol = resolve_policy(
        worktree=wt, control_root=control, quest_id="abc",
        cfg=SandboxConfig(),
        engine_env_result={"IMP_PROJECT_ROOT": str(wt)},
        environ={"PATH": "/usr/bin", "IMP_SPIKE_LOG": "1", "IMP_DEBUG": "verbose"},
        available=True,
    )
    assert ("IMP_SPIKE_LOG", "1") in pol.setenv
    assert ("IMP_DEBUG", "verbose") in pol.setenv
    assert ("IMP_PROJECT_ROOT", str(wt)) in pol.setenv


def test_build_setenv_strips_provider_api_keys(tmp_path):
    # AC 5: provider key env vars are never on the allowlist (not IMP_-prefixed,
    # not generic/LC_/extra_env), so they never reach the agent.
    control, wt = _layout(tmp_path)
    pol = resolve_policy(
        worktree=wt, control_root=control, quest_id="abc",
        cfg=SandboxConfig(),
        engine_env_result={},
        environ={"PATH": "/usr/bin", "ANTHROPIC_API_KEY": "sk-ant",
                 "OPENAI_API_KEY": "sk-oai"},
        available=True,
    )
    keys = {k for k, _ in pol.setenv}
    assert "ANTHROPIC_API_KEY" not in keys
    assert "OPENAI_API_KEY" not in keys


def test_build_setenv_ambient_engine_managed_imp_var_does_not_leak(tmp_path):
    # AC 5: an ambient IMP_TEST_CMD_FAST (an ENGINE_ENV_VARS member) that
    # engine_env_result does NOT provide must NOT leak in — the engine's
    # "absent => no fast gate" contract holds.
    control, wt = _layout(tmp_path)
    pol = resolve_policy(
        worktree=wt, control_root=control, quest_id="abc",
        cfg=SandboxConfig(),
        engine_env_result={"IMP_PROJECT_ROOT": str(wt)},
        environ={"PATH": "/usr/bin", "IMP_TEST_CMD_FAST": "stale-gate"},
        available=True,
    )
    keys = {k for k, _ in pol.setenv}
    assert "IMP_TEST_CMD_FAST" not in keys


def test_build_setenv_engine_imp_var_wins_over_ambient(tmp_path):
    # AC 5: when both ambient and engine provide IMP_TEST_CMD_FAST, the engine
    # value is authoritative (ambient pass excludes ENGINE_ENV_VARS; engine loop
    # runs after it).
    control, wt = _layout(tmp_path)
    pol = resolve_policy(
        worktree=wt, control_root=control, quest_id="abc",
        cfg=SandboxConfig(),
        engine_env_result={"IMP_TEST_CMD_FAST": "real-gate"},
        environ={"PATH": "/usr/bin", "IMP_TEST_CMD_FAST": "stale-gate"},
        available=True,
    )
    assert ("IMP_TEST_CMD_FAST", "real-gate") in pol.setenv
    assert ("IMP_TEST_CMD_FAST", "stale-gate") not in pol.setenv


def test_resolve_policy_uid_zero_masks_run_user_0(tmp_path):
    control, wt = _layout(tmp_path)
    pol = resolve_policy(
        worktree=wt, control_root=control, quest_id="abc",
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
    base = dict(sandbox={},
                test_cmd_fast=None, test_cmd_slow=None,
                test_cmd_fast_timeout=None, test_cmd_slow_timeout=None)
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
        worktree=wt, control_root=control, quest_id="abc",
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


def test_sandbox_config_from_raw_maps_fields_and_defaults() -> None:
    from imp.engine.sandbox import sandbox_config_from_raw

    cfg = sandbox_config_from_raw(
        {"network": False, "extra_env": ["FOO", "BAR"], "allow_unsandboxed": True})
    assert cfg.network is False
    assert cfg.extra_env == ("FOO", "BAR")
    assert cfg.allow_unsandboxed is True
    # fields absent from the raw mapping fall back to the code defaults:
    assert cfg.enabled is True
    assert cfg.secret_paths == ()
    assert cfg.writable_caches == ()
    assert cfg.extra_readable == ()
    assert cfg.extra_writable == ()


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


def _refprot_policy() -> SandboxPolicy:
    # A policy that exercises git-ref protection: the three RO masks live in
    # git_ro_remask (as resolve_policy will append them); refs/heads/imp is the
    # RW re-expose. A secret dir is present so the ordering test has a mask to
    # anchor against.
    return _policy(
        worktree="/wt",
        git_common_dir="/ctl/.git",
        git_ro_remask=("/ctl/.git/hooks", "/ctl/.git/config",
                       "/ctl/.git/refs/heads", "/ctl/.git/refs/stash",
                       "/ctl/.git/index"),
        git_ref_reexpose=("/ctl/.git/refs/heads/imp",),
        writable_caches=(), extra_readable=(), extra_writable=(),
        secret_dirs=("/home/u/.ssh",), secret_files=(),
    )


def test_ref_masks_deny_shared_refs_and_index():
    p = _refprot_policy()
    # A non-imp branch, the stash ref, and the main index are all read-only.
    assert p.allows_write(Path("/ctl/.git/refs/heads/master")) is False
    assert p.allows_write(Path("/ctl/.git/refs/stash")) is False
    assert p.allows_write(Path("/ctl/.git/index")) is False


def test_ref_reexpose_keeps_imp_and_imp_own_git_writes_writable():
    p = _refprot_policy()
    # The agent's own change + spike branches (under refs/heads/imp) stay rw...
    assert p.allows_write(Path("/ctl/.git/refs/heads/imp/nate82")) is True
    assert p.allows_write(Path("/ctl/.git/refs/heads/imp/spike-nate82-1")) is True
    # ...and imp's other in-sandbox .git writes are untouched by the masks:
    assert p.allows_write(Path("/ctl/.git/objects/ab/cdef")) is True
    assert p.allows_write(Path("/ctl/.git/worktrees/nate82/x")) is True
    assert p.allows_write(Path("/ctl/.git/refs/imp/artifacts/nate82")) is True
    assert p.allows_write(Path("/ctl/.git/imp/repo.lock")) is True


def test_ref_reexpose_argv_after_refs_heads_mask_before_secrets():
    p = _refprot_policy()
    argv = build_sandbox_argv("x", cwd="/wt", policy=p, engine_env={})
    flat = " ".join(argv)
    assert "--ro-bind /ctl/.git/refs/heads /ctl/.git/refs/heads" in flat
    assert "--ro-bind /ctl/.git/refs/stash /ctl/.git/refs/stash" in flat
    assert "--ro-bind /ctl/.git/index /ctl/.git/index" in flat
    assert "--bind /ctl/.git/refs/heads/imp /ctl/.git/refs/heads/imp" in flat
    # Ordering (bwrap last-wins): the refs/heads RO mask, then the refs/heads/imp
    # RW re-expose, then the first secret mask. "/ctl/.git/refs/heads" and
    # "/ctl/.git/refs/heads/imp" are distinct tokens, so index() finds each src.
    i_mask = argv.index("/ctl/.git/refs/heads")
    i_reexpose = argv.index("/ctl/.git/refs/heads/imp")
    i_secret = argv.index("--tmpfs")
    assert i_mask < i_reexpose < i_secret


def test_argv_binds_match_bind_plan_with_ref_protection():
    # Drift guard: the new rw category must round-trip through the argv parser.
    p = _refprot_policy()
    argv = build_sandbox_argv("x", cwd="/wt", policy=p, engine_env={})
    assert _parse_argv_binds(argv) == p.bind_plan()
    assert ("/ctl/.git/refs/heads/imp", "rw") in p.bind_plan()


def test_headless_vars_flow_through_build_sandbox_argv():
    # The four headless vars ride the live per-spawn engine_env; the allowlist
    # (_is_engine_passthrough) admits them so they survive --clearenv.
    argv = build_sandbox_argv(
        "x", cwd="/wt", policy=_policy(),
        engine_env={"GIT_EDITOR": "true", "GIT_TERMINAL_PROMPT": "0",
                    "EDITOR": "true", "VISUAL": "true"})
    joined = " ".join(argv)
    assert "--clearenv" in argv
    assert "--setenv GIT_EDITOR true" in joined
    assert "--setenv GIT_TERMINAL_PROMPT 0" in joined
    assert "--setenv EDITOR true" in joined
    assert "--setenv VISUAL true" in joined
    # after --clearenv, not before it
    assert argv.index("--clearenv") < argv.index("GIT_EDITOR")


def test_build_setenv_bakes_headless_vars_from_engine_env(tmp_path):
    control, wt = _layout(tmp_path)
    pol = resolve_policy(
        worktree=wt, control_root=control, quest_id="abc",
        cfg=SandboxConfig(),
        engine_env_result={"IMP_PROJECT_ROOT": str(wt), "GIT_EDITOR": "true",
                           "GIT_TERMINAL_PROMPT": "0", "EDITOR": "true",
                           "VISUAL": "true"},
        environ={"PATH": "/usr/bin"},
        available=True,
    )
    assert ("GIT_EDITOR", "true") in pol.setenv
    assert ("GIT_TERMINAL_PROMPT", "0") in pol.setenv
    assert ("EDITOR", "true") in pol.setenv
    assert ("VISUAL", "true") in pol.setenv


def test_resolve_policy_masks_git_refs_and_reexposes_imp(tmp_path):
    control, wt = _layout(tmp_path)
    git = control / ".git"
    (git / "refs" / "heads").mkdir(parents=True)
    (git / "index").write_text("")
    (git / "refs" / "stash").write_text("")
    pol = resolve_policy(worktree=wt, control_root=control, quest_id="abc",
                         cfg=SandboxConfig(), engine_env_result={}, environ={},
                         available=True)
    # The three crown-jewel paths join the RO remask...
    assert str(git / "refs" / "heads") in pol.git_ro_remask
    assert str(git / "refs" / "stash") in pol.git_ro_remask
    assert str(git / "index") in pol.git_ro_remask
    # ...and refs/heads/imp is the RW re-expose, created on disk so the bind is
    # valid even before the first imp/ loose ref exists.
    assert pol.git_ref_reexpose == (str(git / "refs" / "heads" / "imp"),)
    assert (git / "refs" / "heads" / "imp").is_dir()
    # End-to-end verdicts through the in-process mirror.
    assert pol.allows_write(git / "refs" / "heads" / "master") is False
    assert pol.allows_write(git / "index") is False
    assert pol.allows_write(git / "refs" / "heads" / "imp" / "abc") is True


def test_resolve_policy_skips_absent_ref_masks(tmp_path):
    control, wt = _layout(tmp_path)
    git = control / ".git"
    (git / "refs" / "heads").mkdir(parents=True)
    # No index and no refs/stash on disk — a fresh clone may lack refs/stash.
    pol = resolve_policy(worktree=wt, control_root=control, quest_id="abc",
                         cfg=SandboxConfig(), engine_env_result={}, environ={},
                         available=True)
    assert str(git / "refs" / "heads") in pol.git_ro_remask
    assert str(git / "refs" / "stash") not in pol.git_ro_remask
    assert str(git / "index") not in pol.git_ro_remask
    # The re-expose is still emitted (refs/heads/imp is created unconditionally
    # for a non-draft change).
    assert pol.git_ref_reexpose == (str(git / "refs" / "heads" / "imp"),)
    assert (git / "refs" / "heads" / "imp").is_dir()


def test_resolve_policy_draft_change_skips_ref_protection(tmp_path):
    control, _wt = _layout(tmp_path)
    git = control / ".git"
    (git / "refs" / "heads").mkdir(parents=True)
    (git / "index").write_text("")
    # worktree IS the control root → draft/degenerate case: emit none of it.
    pol = resolve_policy(worktree=control, control_root=control, quest_id="abc",
                         cfg=SandboxConfig(), engine_env_result={}, environ={},
                         available=True)
    assert str(git / "refs" / "heads") not in pol.git_ro_remask
    assert str(git / "refs" / "stash") not in pol.git_ro_remask
    assert str(git / "index") not in pol.git_ro_remask
    assert pol.git_ref_reexpose == ()
    # refs/heads/imp is NOT created for the draft case.
    assert not (git / "refs" / "heads" / "imp").exists()


def test_ephemeral_home_emitted_early_and_writable():
    # A spike-shaped hand-built policy: ephemeral tmpfs home under which the
    # worktree, change_home and a credential dir all live.
    p = _policy(
        worktree="/home/u/proj/.imp/worktrees/abc",
        change_home="/home/u/proj/.imp/changes/abc",
        git_common_dir="/home/u/proj/.git",
        git_ro_remask=(), writable_caches=(), extra_readable=(),
        extra_writable=(), secret_dirs=("/run/user/1000",), secret_files=(),
        ephemeral_home="/home/u",
        cred_writethrough=("/home/u/.claude", "/home/u/.codex"),
    )
    plan = p.bind_plan()
    # The home tmpfs is emitted right after /tmp and BEFORE the worktree bind.
    assert plan[0] == ("/tmp", "rw")
    assert plan[1] == ("/home/u", "tmpfs_rw")
    i_home = plan.index(("/home/u", "tmpfs_rw"))
    i_wt = plan.index(("/home/u/proj/.imp/worktrees/abc", "rw"))
    i_ch = plan.index(("/home/u/proj/.imp/changes/abc", "rw"))
    i_claude = plan.index(("/home/u/.claude", "rw"))
    i_mask = plan.index(("/run/user/1000", "mask_dir"))
    assert i_home < i_wt < i_claude          # tmpfs first, cred write-through later
    assert i_home < i_ch < i_claude
    assert i_claude < i_mask                 # secret masks remain last
    # allows_write: everything under the tmpfs home is writable (re-exposed),
    # the worktree/change_home (under home) win over the tmpfs, and the cred
    # dirs are writable — the only real-home paths a spike can write.
    assert p.allows_write(Path("/home/u/.local/bin/x")) is True
    assert p.allows_write(Path("/home/u/proj/.imp/worktrees/abc/src/a.py")) is True
    assert p.allows_write(Path("/home/u/proj/.imp/changes/abc/progress.md")) is True
    assert p.allows_write(Path("/home/u/.claude/.credentials.json")) is True
    assert p.allows_write(Path("/home/u/.codex/auth.json")) is True
    # /run/user mask (emitted last, absolute) still denies.
    assert p.allows_write(Path("/run/user/1000/bus")) is False


def test_tmpfs_rw_argv_is_identical_to_mask_dir_emission():
    p = _policy(
        worktree="/wt", git_common_dir="/ctl/.git",
        git_ro_remask=(), writable_caches=(), extra_readable=(),
        extra_writable=(), secret_dirs=("/home/u/.ssh",), secret_files=(),
        ephemeral_home="/home/u",
    )
    argv = build_sandbox_argv("x", cwd="/wt", policy=p, engine_env={})
    pairs = list(itertools.pairwise(argv))
    # tmpfs_rw home and the secret mask_dir both emit `--tmpfs <path>`.
    assert ("--tmpfs", "/home/u") in pairs       # ephemeral home
    assert ("--tmpfs", "/home/u/.ssh") in pairs  # secret mask (byte-identical form)


def test_cred_writethrough_argv_is_rw_bind():
    p = _policy(
        worktree="/wt", git_common_dir="/ctl/.git",
        git_ro_remask=(), writable_caches=(), extra_readable=(),
        extra_writable=(), secret_dirs=(), secret_files=(),
        ephemeral_home="/home/u",
        cred_writethrough=("/home/u/.claude",),
    )
    argv = " ".join(build_sandbox_argv("x", cwd="/wt", policy=p, engine_env={}))
    assert "--bind /home/u/.claude /home/u/.claude" in argv


def test_argv_binds_match_bind_plan_spike():
    # Extended argv-parity guard (AC 3). tmpfs_rw and mask_dir emit byte-identical
    # `--tmpfs <path>` argv, so _parse_argv_binds recovers BOTH as "mask_dir";
    # normalize bind_plan's tmpfs_rw to mask_dir before the order/position compare.
    p = _policy(
        worktree="/wt", git_common_dir="/ctl/.git",
        git_ro_remask=("/ctl/.git/hooks",), writable_caches=(),
        extra_readable=(), extra_writable=(),
        secret_dirs=("/home/u/.ssh", "/run/user/1000"), secret_files=(),
        ephemeral_home="/home/u",
        cred_writethrough=("/home/u/.claude",),
    )
    argv = build_sandbox_argv("x", cwd="/wt", policy=p, engine_env={})
    expected = [(b, "mask_dir" if k == "tmpfs_rw" else k) for b, k in p.bind_plan()]
    assert _parse_argv_binds(argv) == expected
    # ...then pin the real drift risk: the two identical-argv kinds have OPPOSITE
    # write semantics — the tmpfs home is writable, the secret mask denies.
    assert p.allows_write(Path("/home/u/.local/bin/x")) is True
    assert p.allows_write(Path("/home/u/.ssh/id_rsa")) is False


def test_resolve_policy_spike_makes_home_a_writable_tmpfs(tmp_path):
    control, wt = _layout(tmp_path)
    home = tmp_path / "home"
    home.mkdir()
    pol = resolve_policy(
        worktree=wt, control_root=control, quest_id="abc",
        cfg=SandboxConfig(home=home), engine_env_result={}, environ={},
        available=True, spike=True,
    )
    assert pol.ephemeral_home == str(home)
    assert (str(home), "tmpfs_rw") in pol.bind_plan()
    # AC 1c: any path under the tmpfs home is writable.
    assert pol.allows_write(home / "anything") is True
    assert pol.allows_write(home / ".local" / "bin" / "x") is True


def test_resolve_policy_spike_reexposes_worktree_and_change_home_under_home(tmp_path):
    # AC 2: project root under $HOME → worktree and change_home re-expose over the
    # early home tmpfs and stay writable.
    home = tmp_path / "home"
    control = home / "proj"
    wt = control / ".imp" / "worktrees" / "abc"
    wt.mkdir(parents=True)
    (control / ".git" / "hooks").mkdir(parents=True)
    (control / ".git" / "config").write_text("")
    change_home = control / ".imp" / "changes" / "abc"
    pol = resolve_policy(
        worktree=wt, control_root=control, quest_id="abc",
        cfg=SandboxConfig(home=home), engine_env_result={}, environ={},
        available=True, change_dir=change_home, spike=True,
    )
    plan = pol.bind_plan()
    assert plan.index((str(home), "tmpfs_rw")) < plan.index((str(wt), "rw"))
    assert pol.allows_write(wt / "src" / "a.py") is True
    assert pol.allows_write(change_home / "progress.md") is True


def test_resolve_policy_spike_binds_existing_cred_dirs_writethrough(tmp_path):
    control, wt = _layout(tmp_path)
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    (home / ".codex").mkdir(parents=True)
    pol = resolve_policy(
        worktree=wt, control_root=control, quest_id="abc",
        cfg=SandboxConfig(home=home), engine_env_result={}, environ={},
        available=True, spike=True,
    )
    assert pol.cred_writethrough == (str(home / ".claude"), str(home / ".codex"))
    # Write-through binds are rw and win over the tmpfs home (emitted after it).
    assert pol.allows_write(home / ".claude" / ".credentials.json") is True
    assert pol.allows_write(home / ".codex" / "auth.json") is True


def test_resolve_policy_spike_cred_dirs_existence_gated(tmp_path):
    control, wt = _layout(tmp_path)
    home = tmp_path / "home"
    home.mkdir()   # neither .claude nor .codex exists
    pol = resolve_policy(
        worktree=wt, control_root=control, quest_id="abc",
        cfg=SandboxConfig(home=home), engine_env_result={}, environ={},
        available=True, spike=True,
    )
    assert pol.cred_writethrough == ()   # nothing bound; spike still starts


def test_resolve_policy_spike_honors_claude_config_dir_override(tmp_path):
    control, wt = _layout(tmp_path)
    home = tmp_path / "home"
    home.mkdir()
    custom = tmp_path / "custom-claude"
    custom.mkdir()
    pol = resolve_policy(
        worktree=wt, control_root=control, quest_id="abc",
        cfg=SandboxConfig(home=home), engine_env_result={},
        environ={"CLAUDE_CONFIG_DIR": str(custom)}, available=True, spike=True,
    )
    assert str(custom) in pol.cred_writethrough
    assert str(home / ".claude") not in pol.cred_writethrough


def test_resolve_policy_spike_omits_home_relative_caches_and_masks(tmp_path):
    control, wt = _layout(tmp_path)
    home = tmp_path / "home"
    (home / ".cache").mkdir(parents=True)     # would be a writable cache when non-spike
    (home / ".ssh").mkdir(parents=True)       # would be a secret mask_dir when non-spike
    (home / ".netrc").write_text("secret")    # would be a secret mask_file when non-spike
    pol = resolve_policy(
        worktree=wt, control_root=control, quest_id="abc",
        cfg=SandboxConfig(home=home, secret_paths=("/abs/secret",)),
        engine_env_result={}, environ={}, available=True, spike=True,
    )
    # Home-relative caches/masks are moot under the tmpfs home → not emitted.
    assert pol.writable_caches == ()
    assert str(home / ".ssh") not in pol.secret_dirs
    assert str(home / ".netrc") not in pol.secret_files
    # /run/user/$UID (absolute) is still masked last.
    assert any(d.startswith("/run/user/") for d in pol.secret_dirs)


def test_resolve_policy_spike_keeps_absolute_secret_paths(tmp_path):
    control, wt = _layout(tmp_path)
    home = tmp_path / "home"
    home.mkdir()
    abs_secret = tmp_path / "abs-secret"
    abs_secret.mkdir()   # absolute dir outside $HOME
    pol = resolve_policy(
        worktree=wt, control_root=control, quest_id="abc",
        cfg=SandboxConfig(home=home, secret_paths=(str(abs_secret),)),
        engine_env_result={}, environ={}, available=True, spike=True,
    )
    assert str(abs_secret) in pol.secret_dirs   # absolute secret still masked


def test_resolve_policy_spike_omits_home_relative_secret_paths(tmp_path):
    # Load-bearing partition: a HOME-RELATIVE cfg.secret_paths entry is moot under
    # the tmpfs home and must be dropped even though it physically exists; the
    # `if spike and not is_abs: continue` guard is the only thing enforcing this.
    control, wt = _layout(tmp_path)
    home = tmp_path / "home"
    (home / ".mykeys").mkdir(parents=True)   # a real, existing home-relative dir
    (home / ".mysecret").write_text("x")     # a real, existing home-relative file
    pol = resolve_policy(
        worktree=wt, control_root=control, quest_id="abc",
        cfg=SandboxConfig(home=home, secret_paths=(".mykeys", ".mysecret")),
        engine_env_result={}, environ={}, available=True, spike=True,
    )
    assert str(home / ".mykeys") not in pol.secret_dirs
    assert str(home / ".mysecret") not in pol.secret_files


def test_resolve_policy_non_spike_default_has_no_spike_binds(tmp_path):
    # AC 6: spike=False (the default) leaves the ephemeral-home / cred binds absent
    # and the existing caches + home-relative masks intact.
    control, wt = _layout(tmp_path)
    home = tmp_path / "home"
    (home / ".cache").mkdir(parents=True)
    (home / ".ssh").mkdir(parents=True)
    (home / ".claude").mkdir(parents=True)
    pol = resolve_policy(
        worktree=wt, control_root=control, quest_id="abc",
        cfg=SandboxConfig(home=home), engine_env_result={}, environ={},
        available=True,   # spike defaults to False
    )
    assert pol.ephemeral_home == ""
    assert pol.cred_writethrough == ()
    assert str(home / ".cache") in pol.writable_caches      # cache still bound
    assert str(home / ".ssh") in pol.secret_dirs            # home mask still emitted


def test_resolve_policy_bakes_workflow_snapshot_overlay(tmp_path):
    control, wt = _layout(tmp_path)
    change_dir = control / ".imp" / "changes" / "abc"
    (change_dir / "workflow").mkdir(parents=True)
    pol = resolve_policy(
        worktree=wt, control_root=control, quest_id="abc",
        cfg=SandboxConfig(), engine_env_result={}, environ={},
        available=True, change_dir=change_dir,
    )
    assert str(change_dir / "workflow") in pol.readonly_overlays


def test_resolve_policy_omits_workflow_overlay_when_snapshot_absent(tmp_path):
    control, wt = _layout(tmp_path)
    change_dir = control / ".imp" / "changes" / "abc"
    change_dir.mkdir(parents=True)   # change home exists, but no workflow/ subtree
    pol = resolve_policy(
        worktree=wt, control_root=control, quest_id="abc",
        cfg=SandboxConfig(), engine_env_result={}, environ={},
        available=True, change_dir=change_dir,
    )
    assert str(change_dir / "workflow") not in pol.readonly_overlays
    assert pol.readonly_overlays == ()


def test_resolve_policy_omits_workflow_overlay_when_change_dir_unset(tmp_path):
    control, wt = _layout(tmp_path)
    pol = resolve_policy(
        worktree=wt, control_root=control, quest_id="abc",
        cfg=SandboxConfig(), engine_env_result={}, environ={},
        available=True,   # change_dir defaults to None
    )
    assert pol.readonly_overlays == ()


def test_resolve_policy_workflow_snapshot_denies_write_siblings_writable(tmp_path):
    control, wt = _layout(tmp_path)
    change_dir = control / ".imp" / "changes" / "abc"
    (change_dir / "workflow").mkdir(parents=True)
    pol = resolve_policy(
        worktree=wt, control_root=control, quest_id="abc",
        cfg=SandboxConfig(), engine_env_result={}, environ={},
        available=True, change_dir=change_dir,
    )
    assert pol.allows_write(change_dir / "workflow" / "workflow.yaml") is False
    assert pol.allows_write(change_dir / "artifacts" / "spec" / "x.md") is True


def test_resolve_policy_workflow_ro_bind_emitted_after_change_home(tmp_path):
    control, wt = _layout(tmp_path)
    change_dir = control / ".imp" / "changes" / "abc"
    snapshot = change_dir / "workflow"
    snapshot.mkdir(parents=True)
    pol = resolve_policy(
        worktree=wt, control_root=control, quest_id="abc",
        cfg=SandboxConfig(), engine_env_result={}, environ={},
        available=True, change_dir=change_dir,
    )
    argv = " ".join(build_sandbox_argv("x", cwd=str(wt), policy=pol, engine_env={}))
    change_home_bind = f"--bind {change_dir} {change_dir}"
    snapshot_bind = f"--ro-bind {snapshot} {snapshot}"
    assert change_home_bind in argv
    assert snapshot_bind in argv
    assert argv.index(change_home_bind) < argv.index(snapshot_bind)


def test_resolve_policy_omits_workflow_overlay_when_workflow_is_plain_file(tmp_path):
    control, wt = _layout(tmp_path)
    change_dir = control / ".imp" / "changes" / "abc"
    change_dir.mkdir(parents=True)
    (change_dir / "workflow").write_text("")   # a plain file, not a directory
    pol = resolve_policy(
        worktree=wt, control_root=control, quest_id="abc",
        cfg=SandboxConfig(), engine_env_result={}, environ={},
        available=True, change_dir=change_dir,
    )
    assert pol.readonly_overlays == ()
