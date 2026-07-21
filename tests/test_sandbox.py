"""Always-on sandbox tests — no bwrap needed (pure argv + policy resolution)."""
from __future__ import annotations

import dataclasses
import os
import tempfile
import types as _types
from pathlib import Path

import pytest

from imp.engine.sandbox import (
    HomeLayer,
    SandboxConfig,
    SandboxPolicy,
    SandboxUnavailable,
    build_sandbox_argv,
    make_home_layer,
    prune_missing_masks,
    resolve_policy,
    sandbox_available,
)


def _policy(**over) -> SandboxPolicy:
    base = dict(
        active=True,
        worktree="/ctl/.imp/worktrees/abc",
        git_common_dir="/ctl/.git",
        git_ro_remask=("/ctl/.git/hooks", "/ctl/.git/config"),
        writable_caches=("/home/u/.cache", "/home/u/.cargo"),
        extra_readable=(),
        extra_writable=(),
        secret_dirs=("/home/u/.ssh", "/run/user/1000"),
        secret_files=("/home/u/.netrc",),
        setenv=(("PATH", "/usr/bin"), ("HOME", "/home/u"),
                ("IMP_PROJECT_ROOT", "/ctl/.imp/worktrees/abc"),
                ("TMPDIR", "/tmp/imp-1000/abc")),
        network=True,
        note="",
    )
    base.update(over)
    return SandboxPolicy(**base)


def test_inactive_policy_returns_bare_argv():
    from imp.engine.sandbox import SANDBOX_ENTERED_MARKER
    argv = build_sandbox_argv("echo hi", cwd="/ctl/.imp/worktrees/abc",
                              policy=_policy(active=False), engine_env={})
    assert argv == ["sh", "-c", "echo hi"]
    assert SANDBOX_ENTERED_MARKER not in argv[-1]


def test_active_policy_wraps_with_bwrap():
    argv = build_sandbox_argv("echo hi", cwd="/ctl/.imp/worktrees/abc",
                              policy=_policy(), engine_env={})
    assert argv[0] == "bwrap"
    assert argv[-3:-1] == ["sh", "-c"]
    assert argv[-1].endswith("; echo hi")          # sentinel-prefixed inner command
    assert argv[1:4] == ["--ro-bind", "/", "/"]   # ro-floor first


def test_active_policy_emits_entry_sentinel_in_tail():
    import shlex

    from imp.engine.sandbox import SANDBOX_ENTERED_MARKER
    argv = build_sandbox_argv("echo hi", cwd="/ctl/.imp/worktrees/abc",
                              policy=_policy(), engine_env={})
    assert argv[-3:-1] == ["sh", "-c"]
    expected_tail = (
        f"printf '%s\\n' {shlex.quote(SANDBOX_ENTERED_MARKER)} >&2; echo hi")
    assert argv[-1] == expected_tail


def test_worktree_and_git_are_rw_bound_at_real_path():
    argv = " ".join(build_sandbox_argv("x", cwd="/wt", policy=_policy(), engine_env={}))
    assert "--bind /ctl/.imp/worktrees/abc /ctl/.imp/worktrees/abc" in argv
    assert "--bind /ctl/.git /ctl/.git" in argv


def test_git_escape_vectors_are_remasked_ro():
    argv = " ".join(build_sandbox_argv("x", cwd="/wt", policy=_policy(), engine_env={}))
    assert "--ro-bind /ctl/.git/hooks /ctl/.git/hooks" in argv
    assert "--ro-bind /ctl/.git/config /ctl/.git/config" in argv


def test_prune_missing_masks_drops_vanished_sibling_paths(tmp_path):
    # A long-running quest outlives a sibling: existence-gated paths captured at run
    # start (steps/ in secret_dirs, events.jsonl in secret_files, a stale path in
    # git_ro_remask) can vanish mid-run. bwrap would fail creating each over the ro
    # floor; prune drops them so later spawns keep running.
    live_steps = tmp_path / "live" / "steps"
    live_steps.mkdir(parents=True)
    gone_steps = tmp_path / "gone" / "steps"          # never created
    live_events = tmp_path / "live" / "events.jsonl"
    live_events.write_text("{}")
    gone_events = tmp_path / "gone" / "events.jsonl"  # never created
    live_cfg = tmp_path / "live.worktree"
    live_cfg.write_text("x")
    gone_cfg = tmp_path / "gone.worktree"             # never created

    p = _policy(
        secret_dirs=(str(live_steps), str(gone_steps)),
        secret_files=(str(live_events), str(gone_events)),
        git_ro_remask=(str(live_cfg), str(gone_cfg)),
    )
    pruned = prune_missing_masks(p)

    assert pruned.secret_dirs == (str(live_steps),)
    assert pruned.secret_files == (str(live_events),)
    assert pruned.git_ro_remask == (str(live_cfg),)
    # The resulting argv references none of the vanished paths.
    argv = " ".join(build_sandbox_argv("x", cwd="/wt", policy=pruned, engine_env={}))
    for gone in (gone_steps, gone_events, gone_cfg):
        assert str(gone) not in argv
    # Live paths still masked.
    assert str(live_steps) in argv
    assert str(live_events) in argv


def test_prune_missing_masks_noop_on_inactive_policy():
    p = _policy(active=False)
    assert prune_missing_masks(p) is p


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
    # Regression (name-on-intent + sandbox): a Quest is created with an empty
    # title, so the policy is frozen with IMP_QUEST_TITLE="" at run start. After
    # rename_quest the live per-spawn engine_env carries the real title;
    # build_sandbox_argv sources IMP_* from it authoritatively, so a title-dependent
    # gate never sees the frozen "".
    pol = _policy(setenv=(("PATH", "/usr/bin"),
                          ("IMP_QUEST_TITLE", ""),
                          ("TMPDIR", "/tmp")))
    argv = build_sandbox_argv(
        "x", cwd="/wt", policy=pol,
        engine_env={"IMP_QUEST_TITLE": "Live mode log polish",
                    "PYTHONPATH": "/wt/src"})
    assert argv.count("IMP_QUEST_TITLE") == 1       # not duplicated
    i = argv.index("IMP_QUEST_TITLE")
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
    assert ("TMPDIR",
            str(Path(tempfile.gettempdir()) / f"imp-{os.getuid()}" / "abc")) in pol.setenv


def test_resolve_policy_masks_worktrees_not_sibling_config(tmp_path):
    control, wt = _layout(tmp_path)               # wt basename == "abc"
    sib = control / ".git" / "worktrees" / "sib"
    sib.mkdir(parents=True)
    (sib / "config.worktree").write_text("")
    pol = resolve_policy(worktree=wt, control_root=control, quest_id="abc",
                         cfg=SandboxConfig(), engine_env_result={}, environ={},
                         available=True,
                         quest_dir=control / ".imp" / "quests" / "abc")
    # The sibling config.worktree is NO LONGER enumerated into git_ro_remask...
    assert str(sib / "config.worktree") not in pol.git_ro_remask
    # ...it is hidden wholesale by the .git/worktrees tmpfs mask; the sibling is
    # unreadable while self's admin dir stays writable.
    assert pol.git_worktrees_mask == str(control / ".git" / "worktrees")
    assert pol.allows_read(sib / "config.worktree") is False
    assert pol.allows_write(control / ".git" / "worktrees" / "abc" / "HEAD") is True


def test_resolve_policy_masks_worktrees_and_reexposes_self(tmp_path):
    control, wt = _layout(tmp_path)               # wt basename == "abc"
    pol = resolve_policy(worktree=wt, control_root=control, quest_id="abc",
                         cfg=SandboxConfig(), engine_env_result={}, environ={},
                         available=True,
                         quest_dir=control / ".imp" / "quests" / "abc")
    git = control / ".git"
    assert pol.git_worktrees_mask == str(git / "worktrees")
    assert pol.git_worktree_self == str(git / "worktrees" / "abc")
    assert pol.allows_write(git / "worktrees" / "abc" / "index") is True
    assert pol.allows_read(git / "worktrees" / "other" / "gitdir") is False


def test_resolve_policy_draft_masks_worktrees_without_self(tmp_path):
    # Draft concierge: worktree IS the control root, quest_dir=None. The mask is
    # emitted (prune vector closed) but there is NO self re-expose — the main
    # checkout is never registered under .git/worktrees, and a self re-expose of a
    # nonexistent path would fail bwrap setup.
    control, _wt = _layout(tmp_path)
    pol = resolve_policy(worktree=control, control_root=control, quest_id="abc",
                         cfg=SandboxConfig(), engine_env_result={}, environ={},
                         available=True, quest_dir=None)
    git = control / ".git"
    assert pol.git_worktrees_mask == str(git / "worktrees")
    assert pol.git_worktree_self == ""
    assert pol.allows_read(git / "worktrees" / "abc" / "HEAD") is False


def test_resolve_policy_unmaterialized_draft_masks_worktrees_without_self(tmp_path):
    # An un-materialized draft quest has a REAL quest_dir but its worktree IS the
    # control root (no registered worktree under .git/worktrees yet — the executor
    # resolves this policy during the brainstorm phase, before go-live). Gating the
    # self re-expose on `quest_dir is not None` ALONE would emit a nonexistent
    # .git/worktrees/<root-basename> rw bind that fails bwrap setup; the added
    # `worktree != control_root` clause leaves it empty here.
    control, _wt = _layout(tmp_path)
    pol = resolve_policy(worktree=control, control_root=control, quest_id="abc",
                         cfg=SandboxConfig(), engine_env_result={}, environ={},
                         available=True,
                         quest_dir=control / ".imp" / "quests" / "abc")
    git = control / ".git"
    assert pol.git_worktrees_mask == str(git / "worktrees")
    assert pol.git_worktree_self == ""
    assert pol.allows_read(git / "worktrees" / "abc" / "HEAD") is False


def test_prune_missing_masks_keeps_worktrees_mask_even_if_absent(tmp_path):
    # The mask + self re-expose are retained unconditionally by prune_missing_masks
    # (like control_imp / git_ref_reexpose), NOT existence-gated like git_ro_remask,
    # so the mask cannot silently drop mid-run. Contrast a vanished git_ro_remask
    # entry, which IS dropped.
    gone = tmp_path / "nope" / ".git" / "worktrees"
    gone_ro = tmp_path / "gone.worktree"
    p = _policy(
        git_worktrees_mask=str(gone),
        git_worktree_self=str(gone / "self"),
        git_ro_remask=(str(gone_ro),),
    )
    pruned = prune_missing_masks(p)
    assert pruned.git_ro_remask == ()
    assert pruned.git_worktrees_mask == str(gone)
    assert pruned.git_worktree_self == str(gone / "self")


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


def test_overlay_available_true_when_mount_succeeds(tmp_path, monkeypatch):
    import imp.engine.sandbox as sb
    monkeypatch.setattr(sb.platform, "system", lambda: "Linux")
    monkeypatch.setattr(sb.shutil, "which", lambda _: "/usr/bin/unshare")
    calls: dict = {}

    def fake_run(argv, **kw):
        calls["argv"] = argv
        return _types.SimpleNamespace(returncode=0)

    monkeypatch.setattr(sb.subprocess, "run", fake_run)
    sb.overlay_available.cache_clear()
    assert sb.overlay_available(tmp_path, tmp_path / "home") is True
    # probes via unshare with a real overlay mount, then removes its scratch dir
    assert calls["argv"][0] == "unshare"
    assert "mount -t overlay" in calls["argv"][-1]
    assert not (tmp_path / ".overlay-probe").exists()


def test_overlay_available_false_when_mount_fails(tmp_path, monkeypatch):
    import imp.engine.sandbox as sb
    monkeypatch.setattr(sb.platform, "system", lambda: "Linux")
    monkeypatch.setattr(sb.shutil, "which", lambda _: "/usr/bin/unshare")
    monkeypatch.setattr(sb.subprocess, "run",
                        lambda *a, **k: _types.SimpleNamespace(returncode=32))
    sb.overlay_available.cache_clear()
    assert sb.overlay_available(tmp_path, tmp_path / "home") is False
    assert not (tmp_path / ".overlay-probe").exists()


def test_overlay_available_false_without_unshare(tmp_path, monkeypatch):
    import imp.engine.sandbox as sb
    monkeypatch.setattr(sb.platform, "system", lambda: "Linux")
    monkeypatch.setattr(sb.shutil, "which", lambda _: None)
    sb.overlay_available.cache_clear()
    assert sb.overlay_available(tmp_path, tmp_path / "home") is False


def test_overlay_available_false_on_non_linux(tmp_path, monkeypatch):
    import imp.engine.sandbox as sb
    monkeypatch.setattr(sb.platform, "system", lambda: "Darwin")
    sb.overlay_available.cache_clear()
    assert sb.overlay_available(tmp_path, tmp_path / "home") is False


def test_overlay_available_false_when_subprocess_raises(tmp_path, monkeypatch):
    import imp.engine.sandbox as sb
    monkeypatch.setattr(sb.platform, "system", lambda: "Linux")
    monkeypatch.setattr(sb.shutil, "which", lambda _: "/usr/bin/unshare")

    def boom(*a, **k):
        raise OSError("no userns")

    monkeypatch.setattr(sb.subprocess, "run", boom)
    sb.overlay_available.cache_clear()
    assert sb.overlay_available(tmp_path, tmp_path / "home") is False
    assert not (tmp_path / ".overlay-probe").exists()


def test_overlay_available_probes_with_real_home_as_lowerdir(tmp_path, monkeypatch):
    import imp.engine.sandbox as sb
    monkeypatch.setattr(sb.platform, "system", lambda: "Linux")
    monkeypatch.setattr(sb.shutil, "which", lambda _: "/usr/bin/unshare")
    calls: dict = {}

    def fake_run(argv, **kw):
        calls["argv"] = argv
        return _types.SimpleNamespace(returncode=0)

    monkeypatch.setattr(sb.subprocess, "run", fake_run)
    sb.overlay_available.cache_clear()
    home = tmp_path / "agent-home"
    assert sb.overlay_available(tmp_path, home) is True
    mount_cmd = calls["argv"][-1]
    # probes with the REAL home as lowerdir — the same shape the per-command mount
    # in build_sandbox_argv uses — not a synthetic empty lower under the probe dir.
    assert f"lowerdir={home}" in mount_cmd
    assert str(tmp_path / ".overlay-probe" / "lower") not in mount_cmd


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


def test_build_setenv_dedups_overlapping_env_passthrough(tmp_path):
    control, wt = _layout(tmp_path)
    pol = resolve_policy(
        worktree=wt, control_root=control, quest_id="abc",
        cfg=SandboxConfig(env_passthrough=("TMPDIR",)),
        engine_env_result={},
        environ={"PATH": "/usr/bin", "TMPDIR": "/var/tmp"},
        available=True,
    )
    # The per-quest scratch dir override must win over the env_passthrough TMPDIR.
    assert ("TMPDIR",
            str(Path(tempfile.gettempdir()) / f"imp-{os.getuid()}" / "abc")) in pol.setenv
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


def test_build_setenv_drops_ambient_imp_vars(tmp_path):
    # AC 1-3: a non-engine IMP_* var set in the launching environment must NOT
    # reach the sandboxed agent; an engine-managed var still arrives
    # authoritatively from engine_env_result; a var named in env_passthrough and
    # present in the environment does pass through.
    control, wt = _layout(tmp_path)
    pol = resolve_policy(
        worktree=wt, control_root=control, quest_id="abc",
        cfg=SandboxConfig(env_passthrough=("IMP_ALLOWED",)),
        engine_env_result={"IMP_PROJECT_ROOT": str(wt)},
        environ={"PATH": "/usr/bin", "IMP_SPIKE_LOG": "1", "IMP_DEBUG": "verbose",
                 "IMP_ALLOWED": "yes"},
        available=True,
    )
    keys = {k for k, _ in pol.setenv}
    assert "IMP_SPIKE_LOG" not in keys
    assert "IMP_DEBUG" not in keys
    assert ("IMP_PROJECT_ROOT", str(wt)) in pol.setenv
    assert ("IMP_ALLOWED", "yes") in pol.setenv


def test_build_setenv_strips_provider_api_keys(tmp_path):
    # AC 5: provider key env vars are never on the allowlist (not IMP_-prefixed,
    # not generic/LC_/env_passthrough), so they never reach the agent.
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
    # AC 1-2: an ambient IMP_TEST_CMD_FAST that engine_env_result does NOT provide
    # must NOT leak in — ambient IMP_* is never passed through, so the engine's
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
    # AC 2: when both ambient and engine provide IMP_TEST_CMD_FAST, only the engine
    # value reaches the agent (ambient IMP_* is never passed through; the engine
    # loop is the sole IMP_* source).
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


def _fake_quest(tmp_path):
    control = tmp_path / "ctl"
    wt = control / ".imp" / "worktrees" / "abc"
    wt.mkdir(parents=True, exist_ok=True)
    (control / ".git").mkdir(parents=True, exist_ok=True)
    return _types.SimpleNamespace(
        project_root=wt, control_root=control, id="abc",
        handle="abc-slug", title="Abc Title",
        quest_dir=control / ".imp" / "quests" / "abc",
        tmp_dir=Path(tempfile.gettempdir()) / f"imp-{os.getuid()}" / "abc")


def test_fake_quest_home_roots_at_control_root(tmp_path):
    quest = _fake_quest(tmp_path)
    assert quest.quest_dir == quest.control_root / ".imp" / "quests" / "abc"
    assert ".imp/worktrees" not in str(quest.quest_dir)


def _fake_config(**over):
    base = dict(sandbox={},
                test_cmd_fast=None, test_cmd_slow=None,
                test_cmd_fast_timeout=None, test_cmd_slow_timeout=None,
                pythonpath=())
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
    def append_agent_message(self, **_kw): ...
    def append_tool_call(self, **_kw): ...
    def append_tool_result(self, **_kw): ...
    def update_last_agent_message_usage(self, **_kw): ...


@pytest.mark.real_sandbox_policy
def test_build_sandbox_policy_hard_fails_when_unavailable(tmp_path, monkeypatch):
    """enabled + unavailable + allow_unsandboxed unset → the run must NOT silently
    proceed unsandboxed: _build_sandbox_policy re-raises SandboxUnavailable (B.7)."""
    import imp.engine.executor as ex
    monkeypatch.setattr(ex, "sandbox_available", lambda: False)
    # Clear the lru_cache so the monkeypatched function is actually used.
    from imp.engine import sandbox as _sb
    _sb.sandbox_available.cache_clear()

    quest = _fake_quest(tmp_path)
    config = _fake_config(sandbox={})      # enabled defaults True, allow_unsandboxed False
    log = _CaptureLog()
    with pytest.raises(SandboxUnavailable):
        ex._build_sandbox_policy(quest, config, log)
    assert any(e["type"] == "sandbox_unavailable" for e in log.events)


@pytest.mark.real_sandbox_policy
def test_build_sandbox_policy_bare_when_allow_unsandboxed(tmp_path, monkeypatch):
    import imp.engine.executor as ex
    monkeypatch.setattr(ex, "sandbox_available", lambda: False)
    from imp.engine import sandbox as _sb
    _sb.sandbox_available.cache_clear()

    quest = _fake_quest(tmp_path)
    config = _fake_config(sandbox={"allow_unsandboxed": True})
    log = _CaptureLog()
    pol = ex._build_sandbox_policy(quest, config, log)
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

    quest = _fake_quest(tmp_path)
    config = _fake_config(sandbox={})
    log = _CaptureLog()
    pol = ex._build_sandbox_policy(quest, config, log)
    assert pol.active is True and pol.note == ""
    assert pol.worktree == str(quest.project_root)
    assert ("IMP_PROJECT_ROOT", str(quest.project_root)) in pol.setenv
    # active is the silent common case — no posture event is emitted.
    assert not any(e["type"] in ("sandbox_inactive", "sandbox_unavailable")
                   for e in log.events)


@pytest.mark.real_sandbox_policy
def test_build_sandbox_policy_threads_pythonpath_to_setenv(tmp_path, monkeypatch):
    """config.pythonpath reaches the sandbox policy's setenv as a worktree-resolved
    PYTHONPATH (imp-on-imp shadow through the general path)."""
    import imp.engine.executor as ex
    monkeypatch.setattr(ex, "sandbox_available", lambda: True)
    from imp.engine import sandbox as _sb
    _sb.sandbox_available.cache_clear()

    quest = _fake_quest(tmp_path)
    config = _fake_config(pythonpath=("src",))
    log = _CaptureLog()
    pol = ex._build_sandbox_policy(quest, config, log)
    # `_build_sandbox_policy` overlays engine_env onto the real `os.environ`, and
    # overlay_engine_env APPENDS any ambient PYTHONPATH after the prefix. The
    # Makefile sets PYTHONPATH for every pytest run, so setenv's PYTHONPATH is a
    # joined string, not the bare prefix — assert the prefix is FIRST (the robust
    # idiom the isolated test below also uses), not an exact (key, value) pair.
    assert dict(pol.setenv)["PYTHONPATH"].startswith(
        str(quest.project_root / "src"))


@pytest.mark.real_sandbox_policy
def test_build_sandbox_policy_writes_run_shim(tmp_path, monkeypatch):
    """The per-run `imp` shim is written at run startup, even on the unsandboxed
    posture (before the policy is resolved)."""
    import imp.engine.executor as ex
    monkeypatch.setattr(ex, "sandbox_available", lambda: False)
    from imp.engine import sandbox as _sb
    _sb.sandbox_available.cache_clear()

    quest = _fake_quest(tmp_path)
    config = _fake_config(sandbox={"allow_unsandboxed": True})
    ex._build_sandbox_policy(quest, config, _CaptureLog())
    shim = quest.quest_dir / "imp-shim" / "imp"
    assert shim.exists()
    assert shim.stat().st_mode & 0o111


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
    """Recover the dest-keyed (path, kind) path-covering binds from a real build_sandbox_argv
    emission — the inverse of build_sandbox_argv's kind→flag map. Scans the bind
    block between the step-1 ro-floor (--ro-bind / /) and step-11 (--proc),
    classifying each flag back to a bind_plan kind."""
    i = argv.index("--ro-bind") + 3        # skip "bwrap" + the "--ro-bind / /" floor
    stop = argv.index("--proc")
    out: list[tuple[str, str]] = []
    while i < stop:
        flag = argv[i]
        if flag == "--bind":
            out.append((argv[i + 2], "rw"))   # DEST-keyed (src != dest for /var/tmp)
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


def test_home_layer_rw_binds_real_home_first():
    # AC 8 (unit): the fallback writable-home layer emits a rw bind of the real
    # home, ordered BEFORE the worktree / cache / secret binds so later binds win
    # (caches write through, worktree stays real, secrets still mask).
    pol = _policy(home="/home/u", home_layer=HomeLayer(home="/home/u"))
    argv = build_sandbox_argv("x", cwd="/wt", policy=pol, engine_env={})
    assert argv[0] == "bwrap"                      # no unshare wrapper for rw fallback
    flat = " ".join(argv)
    assert "--bind /home/u /home/u" in flat
    i_home = argv.index("/home/u")                 # first "/home/u" is the bind source
    i_wt = argv.index("/ctl/.imp/worktrees/abc")
    i_secret = argv.index("/home/u/.ssh")
    assert i_home < i_wt < i_secret


def test_home_layer_makes_home_writable_in_mirror():
    pol = _policy(home="/home/u", home_layer=HomeLayer(home="/home/u"))
    assert pol.allows_write(Path("/home/u/newfile"))          # home is writable
    assert pol.allows_write(Path("/home/u/.cache/x"))         # cache write-through wins
    assert not pol.allows_write(Path("/home/u/.ssh/id_rsa"))  # secret mask still denies
    assert not pol.allows_read(Path("/home/u/.ssh/id_rsa"))   # and unreadable


def test_no_home_layer_leaves_home_read_only():
    # Without a home layer (script gates / pre-attach) home stays read-through the
    # ro floor: no rw home bind, and the mirror denies home writes.
    pol = _policy(home="/home/u")
    flat = " ".join(build_sandbox_argv("x", cwd="/wt", policy=pol, engine_env={}))
    assert "--bind /home/u /home/u" not in flat
    assert not pol.allows_write(Path("/home/u/newfile"))


def test_argv_binds_match_bind_plan_with_home_layer():
    # Drift guard extended to the home bind: the rw fallback's source == dest, so
    # the parse-back still equals bind_plan() (which is dest-keyed).
    pol = _policy(home="/home/u", home_layer=HomeLayer(home="/home/u"))
    argv = build_sandbox_argv("x", cwd="/wt", policy=pol, engine_env={})
    assert _parse_argv_binds(argv) == pol.bind_plan()


def test_overlay_home_wraps_bwrap_with_unshare_mount():
    # AC 1/2: a COW overlay is composed by wrapping bwrap with an unprivileged
    # overlay mount; the merged view is bound onto the REAL home path.
    hl = HomeLayer(home="/home/u", merged="/up/merged",
                   upper="/up/upper", work="/up/work")
    pol = _policy(home="/home/u", home_layer=hl)
    argv = build_sandbox_argv("echo hi", cwd="/wt", policy=pol, engine_env={})
    assert argv[:6] == ["unshare", "--user", "--map-root-user", "--mount",
                        "sh", "-c"]
    script = argv[6]
    assert ("mount -t overlay overlay -o "
            "lowerdir=/home/u,upperdir=/up/upper,workdir=/up/work "
            "/up/merged") in script
    # Fail-loud: on mount failure the wrapper exits nonzero BEFORE the exec,
    # WITHOUT running the command (sentinel absence is the signal); on success it
    # execs bwrap (exec, not a child bwrap, keeps pgid == pid so signals reach the tree).
    assert " && exec" not in script
    assert "|| exit" in script
    assert "exec bwrap" in script
    # merged view bound onto the real home: source == merged, dest == home.
    assert "--bind /up/merged /home/u" in script


def test_overlay_home_shell_quotes_paths_with_spaces():
    hl = HomeLayer(home="/ho me", merged="/a b/merged",
                   upper="/a b/upper", work="/a b/work")
    pol = _policy(home="/ho me", home_layer=hl)
    argv = build_sandbox_argv("x", cwd="/wt", policy=pol, engine_env={})
    script = argv[6]
    assert "lowerdir='/ho me'" in script          # lower (real home) quoted
    assert "upperdir='/a b/upper'" in script       # upper quoted
    assert "workdir='/a b/work'" in script         # work quoted
    assert "'/a b/merged'" in script               # mount target + bind source quoted


def test_overlay_home_write_mirror_is_dest_keyed():
    # bind_plan keys the home entry by the DEST (home), not the merged source, so
    # allows_write is correct under the overlay too.
    hl = HomeLayer(home="/home/u", merged="/up/merged",
                   upper="/up/upper", work="/up/work")
    pol = _policy(home="/home/u", home_layer=hl)
    assert pol.allows_write(Path("/home/u/newfile"))
    assert not pol.allows_write(Path("/home/u/.ssh/id_rsa"))


def test_rw_fallback_home_layer_is_not_wrapped():
    # is_overlay is False when there is no upper → plain bwrap, no unshare wrapper.
    pol = _policy(home="/home/u", home_layer=HomeLayer(home="/home/u"))
    argv = build_sandbox_argv("x", cwd="/wt", policy=pol, engine_env={})
    assert argv[0] == "bwrap"


def test_sandbox_spawn_failed_marker_absence_guarded():
    from imp.engine.sandbox import (
        SANDBOX_ENTERED_MARKER,
        sandbox_spawn_failed,
    )
    present = f"noise\n{SANDBOX_ENTERED_MARKER}\nmore"
    # marker present -> the inner command ran -> not a failure
    assert sandbox_spawn_failed(present, sandbox_active=True) is False
    # marker absent + sandbox active -> the sandbox never entered -> a failure
    assert sandbox_spawn_failed("boom", sandbox_active=True) is True
    assert sandbox_spawn_failed("", sandbox_active=True) is True
    # bare (inactive) spawn: no marker is ever emitted, so absence is NORMAL
    assert sandbox_spawn_failed("boom", sandbox_active=False) is False
    assert sandbox_spawn_failed(present, sandbox_active=False) is False


def test_strip_sandbox_marker_removes_single_line():
    from imp.engine.sandbox import SANDBOX_ENTERED_MARKER, strip_sandbox_marker
    raw = f"{SANDBOX_ENTERED_MARKER}\nreal line\nsecond"
    assert strip_sandbox_marker(raw) == "real line\nsecond"
    # idempotent: a second strip (marker already gone) is a no-op
    assert strip_sandbox_marker(strip_sandbox_marker(raw)) == "real line\nsecond"
    # no marker present -> unchanged
    assert strip_sandbox_marker("just output\n") == "just output\n"


def test_overlay_setup_failed_keys_on_marker():
    from imp.engine.sandbox import (
        OVERLAY_SETUP_FAILED_MARKER,
        overlay_setup_failed,
    )
    # True only when the controlled sentinel is present — NOT on the kernel's
    # own mount-error text (which the wrapper also lets through to stderr).
    assert overlay_setup_failed(f"noise\n{OVERLAY_SETUP_FAILED_MARKER}\nmore") is True
    assert overlay_setup_failed(
        "mount: wrong fs type, bad option, bad superblock on overlay") is False
    assert overlay_setup_failed("") is False


def test_overlay_wrapper_omits_sentinel_on_mount_failure():
    from imp.engine.sandbox import SANDBOX_ENTERED_MARKER
    hl = HomeLayer(home="/home/u", merged="/up/merged",
                   upper="/up/upper", work="/up/work")
    pol = _policy(home="/home/u", home_layer=hl)
    argv = build_sandbox_argv("echo hi", cwd="/wt", policy=pol, engine_env={})
    assert argv[:6] == ["unshare", "--user", "--map-root-user", "--mount",
                        "sh", "-c"]
    script = argv[6]
    assert " && exec" not in script
    assert "|| exit" in script
    assert "exec bwrap" in script
    assert ">&2" in script
    assert SANDBOX_ENTERED_MARKER in script
    assert script.index("exec bwrap") < script.index(SANDBOX_ENTERED_MARKER)


def test_make_home_layer_overlay_builds_dirs_and_cleanup(tmp_path):
    # AC 1 (unit): the overlay layer creates upper/work/merged on disk and returns
    # a HomeLayer that reports is_overlay; the cleanup callable removes all three.
    upper, work, merged = tmp_path / "u", tmp_path / "w", tmp_path / "m"
    layer, cleanup = make_home_layer(
        "/home/u", upper=upper, work=work, merged=merged, use_overlay=True)
    assert isinstance(layer, HomeLayer) and layer.is_overlay
    assert layer.home == "/home/u"
    assert layer.upper == str(upper) and layer.work == str(work)
    assert layer.merged == str(merged)
    assert upper.is_dir() and work.is_dir() and merged.is_dir()
    cleanup()
    assert not upper.exists() and not work.exists() and not merged.exists()


def test_make_home_layer_fallback_builds_no_dirs(tmp_path):
    # AC 8 (unit): the rw fallback layer touches no disk (no overlay dirs) and its
    # cleanup is a harmless no-op.
    upper, work, merged = tmp_path / "u", tmp_path / "w", tmp_path / "m"
    layer, cleanup = make_home_layer(
        "/home/u", upper=upper, work=work, merged=merged, use_overlay=False)
    assert not layer.is_overlay and layer.home == "/home/u"
    assert not upper.exists() and not work.exists() and not merged.exists()
    cleanup()  # no-op, must not raise


def test_resolve_policy_binds_quest_home_rw(tmp_path):
    control, wt = _layout(tmp_path)
    home = control / ".imp" / "quests" / "abc"
    pol = resolve_policy(
        worktree=wt, control_root=control, quest_id="abc",
        cfg=SandboxConfig(), engine_env_result={}, environ={},
        available=True, quest_dir=home,
    )
    assert pol.quest_home == str(home)
    # An artifact write under the home is permitted by the in-process mirror...
    assert pol.allows_write(home / "artifacts" / "spec" / "x.md")
    # ...and the bwrap argv carries the matching rw bind.
    argv = " ".join(build_sandbox_argv("x", cwd=str(wt), policy=pol, engine_env={}))
    assert f"--bind {home} {home}" in argv


def test_resolve_policy_masks_own_steps_not_siblings(tmp_path):
    control, wt = _layout(tmp_path)
    quest_dir = control / ".imp" / "quests" / "abc"
    (quest_dir / "steps").mkdir(parents=True)
    (quest_dir / "artifacts").mkdir()
    (quest_dir / "events.jsonl").write_text("")
    sib_dir = control / ".imp" / "quests" / "sib"
    (sib_dir / "steps").mkdir(parents=True)
    (sib_dir / "events.jsonl").write_text("")
    pol = resolve_policy(
        worktree=wt, control_root=control, quest_id="abc",
        cfg=SandboxConfig(), engine_env_result={}, environ={},
        available=True, quest_dir=quest_dir,
    )
    # Own steps masked by fixed name...
    assert str(quest_dir / "steps") in pol.secret_dirs
    # ...and NO sibling path appears anywhere in the resolved policy.
    assert str(sib_dir / "steps") not in pol.secret_dirs
    assert str(sib_dir / "events.jsonl") not in pol.secret_files
    assert not any("sib" in b for b, _ in pol.bind_plan())
    # The sibling is nonetheless denied in-mirror by the whole-.imp tmpfs.
    assert pol.allows_read(sib_dir / "steps" / "x") is False
    assert pol.allows_read(sib_dir / "events.jsonl") is False


def test_resolve_policy_allows_read_reflects_steps_mask(tmp_path):
    control, wt = _layout(tmp_path)
    quest_dir = control / ".imp" / "quests" / "abc"
    (quest_dir / "steps").mkdir(parents=True)
    (quest_dir / "artifacts").mkdir()
    (quest_dir / "workflow").mkdir()
    (quest_dir / "events.jsonl").write_text("")
    sib_dir = control / ".imp" / "quests" / "sib"
    (sib_dir / "steps").mkdir(parents=True)
    pol = resolve_policy(
        worktree=wt, control_root=control, quest_id="abc",
        cfg=SandboxConfig(), engine_env_result={}, environ={},
        available=True, quest_dir=quest_dir,
    )
    assert pol.allows_read(quest_dir / "steps" / "phase" / "x") is False
    assert pol.allows_read(quest_dir / "artifacts" / "spec.md") is True
    assert pol.allows_read(quest_dir / "workflow" / "w.md") is True
    assert pol.allows_read(quest_dir / "events.jsonl") is False


def test_resolve_policy_steps_mask_is_existence_gated(tmp_path):
    control, wt = _layout(tmp_path)
    quest_dir = control / ".imp" / "quests" / "abc"
    quest_dir.mkdir(parents=True)
    # No steps/ subdirectory created — the existence-gate must produce no entry.
    pol = resolve_policy(
        worktree=wt, control_root=control, quest_id="abc",
        cfg=SandboxConfig(), engine_env_result={}, environ={},
        available=True, quest_dir=quest_dir,
    )
    assert str(quest_dir / "steps") not in pol.secret_dirs


def test_resolve_policy_masks_own_events_not_siblings(tmp_path):
    control, wt = _layout(tmp_path)
    quest_dir = control / ".imp" / "quests" / "abc"
    quest_dir.mkdir(parents=True)
    (quest_dir / "events.jsonl").write_text('{"e":1}\n')
    sib_dir = control / ".imp" / "quests" / "sib"
    sib_dir.mkdir(parents=True)
    (sib_dir / "events.jsonl").write_text("")
    pol = resolve_policy(
        worktree=wt, control_root=control, quest_id="abc",
        cfg=SandboxConfig(), engine_env_result={}, environ={},
        available=True, quest_dir=quest_dir,
    )
    assert str(quest_dir / "events.jsonl") in pol.secret_files
    assert str(sib_dir / "events.jsonl") not in pol.secret_files
    assert pol.allows_read(quest_dir / "events.jsonl") is False


def test_resolve_policy_events_mask_is_existence_gated(tmp_path):
    control, wt = _layout(tmp_path)
    quest_dir = control / ".imp" / "quests" / "abc"
    quest_dir.mkdir(parents=True)
    # No events.jsonl written — the existence-gate must produce no entry.
    pol = resolve_policy(
        worktree=wt, control_root=control, quest_id="abc",
        cfg=SandboxConfig(), engine_env_result={}, environ={},
        available=True, quest_dir=quest_dir,
    )
    assert str(quest_dir / "events.jsonl") not in pol.secret_files


def test_readonly_overlay_denies_write_inside_allows_sibling():
    p = _policy(
        worktree="/wt", git_common_dir="/ctl/.git",
        git_ro_remask=(), writable_caches=(), extra_readable=(),
        extra_writable=(), secret_dirs=(), secret_files=(),
        readonly_overlays=("/wt/src",),
    )
    assert p.allows_write(Path("/wt/src/app.py")) is False     # inside the overlay
    assert p.allows_write(Path("/wt/docs/readme.md")) is True   # sibling stays rw


def test_readonly_overlay_whole_worktree_keeps_quest_home_writable():
    p = _policy(
        worktree="/ctl/.imp/worktrees/abc",
        quest_home="/ctl/.imp/quests/abc",
        git_common_dir="/ctl/.git",
        git_ro_remask=(), writable_caches=(), extra_readable=(),
        extra_writable=(), secret_dirs=(), secret_files=(),
        readonly_overlays=("/ctl/.imp/worktrees/abc",),
    )
    # Whole-worktree ro overlay: source is locked...
    assert p.allows_write(Path("/ctl/.imp/worktrees/abc/src/x.py")) is False
    # ...but quest_home lives OUTSIDE the worktree, so it stays rw.
    assert p.allows_write(Path("/ctl/.imp/quests/abc/progress.md")) is True


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
        {"network": False, "env_passthrough": ["FOO", "BAR"], "allow_unsandboxed": True})
    assert cfg.network is False
    assert cfg.env_passthrough == ("FOO", "BAR")
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


def test_resolve_policy_draft_change_masks_git_refs_and_reexposes_imp(tmp_path):
    # The draft/degenerate case (worktree IS the
    # control root — the threshold concierge and its non-isolated explorer/
    # researcher subagents) now gets the SAME ref-mask as a real worktree, so a
    # raw `git commit` from bash cannot bypass the draft concierge's read-only
    # contract with the project tree. Only refs/heads/imp stays writable.
    control, _wt = _layout(tmp_path)
    git = control / ".git"
    (git / "refs" / "heads").mkdir(parents=True)
    (git / "index").write_text("")
    (git / "refs" / "stash").write_text("")
    pol = resolve_policy(worktree=control, control_root=control, quest_id="abc",
                         cfg=SandboxConfig(), engine_env_result={}, environ={},
                         available=True)
    # The three crown-jewel paths join the RO remask even in draft mode...
    assert str(git / "refs" / "heads") in pol.git_ro_remask
    assert str(git / "refs" / "stash") in pol.git_ro_remask
    assert str(git / "index") in pol.git_ro_remask
    # ...and refs/heads/imp is the RW re-expose, created on disk for the bind.
    assert pol.git_ref_reexpose == (str(git / "refs" / "heads" / "imp"),)
    assert (git / "refs" / "heads" / "imp").is_dir()
    # End-to-end verdicts: master + index read-only, imp/ subtree writable.
    assert pol.allows_write(git / "refs" / "heads" / "master") is False
    assert pol.allows_write(git / "index") is False
    assert pol.allows_write(git / "refs" / "heads" / "imp" / "abc") is True


def test_draft_ref_mask_emitted_in_bwrap_argv(tmp_path):
    # The draft ref-mask survives all the way into the
    # emitted bwrap argv, not just the resolved-policy verdict, so a future
    # bind_plan/build_sandbox_argv refactor that drops it is caught.
    control, _wt = _layout(tmp_path)
    git = control / ".git"
    (git / "refs" / "heads").mkdir(parents=True)
    (git / "index").write_text("")
    (git / "refs" / "stash").write_text("")
    pol = resolve_policy(worktree=control, control_root=control, quest_id="abc",
                         cfg=SandboxConfig(), engine_env_result={}, environ={},
                         available=True)
    argv = build_sandbox_argv(
        "git status", cwd=str(control), policy=pol, engine_env={}
    )
    flat = " ".join(argv)
    assert (
        f"--ro-bind {git / 'refs' / 'heads'} {git / 'refs' / 'heads'}" in flat
    )
    assert (
        f"--bind {git / 'refs' / 'heads' / 'imp'} "
        f"{git / 'refs' / 'heads' / 'imp'}" in flat
    )
    # last-wins ordering: the refs/heads RO mask precedes the refs/heads/imp
    # RW re-expose so the imp/ subtree stays writable.
    i_mask = argv.index(str(git / "refs" / "heads"))
    i_reexpose = argv.index(str(git / "refs" / "heads" / "imp"))
    assert i_mask < i_reexpose


def test_resolve_policy_disposable_tree_uses_universal_home_model(tmp_path):
    # AC 4 + AC 10 (unit): disposable_tree no longer specializes the home — the
    # writable caches write through and the home secret masks apply exactly as
    # for a normal agent; no ephemeral tmpfs home is synthesized.
    control, wt = _layout(tmp_path)
    home = tmp_path / "home"
    (home / ".cache").mkdir(parents=True)
    (home / ".ssh").mkdir(parents=True)
    (home / ".netrc").write_text("secret")
    (home / ".customsecret").mkdir(parents=True)
    pol = resolve_policy(
        worktree=wt, control_root=control, quest_id="abc",
        cfg=SandboxConfig(home=home, secret_paths=(".customsecret",)),
        engine_env_result={}, environ={}, available=True, disposable_tree=True,
    )
    assert pol.disposable_tree is True
    assert str(home / ".cache") in pol.writable_caches       # cache write-through
    assert str(home / ".ssh") in pol.secret_dirs             # home mask applied
    assert str(home / ".netrc") in pol.secret_files          # home file mask applied
    assert str(home / ".customsecret") in pol.secret_dirs    # home-relative cfg secret honored
    # /run/user/$UID still masked last; no tmpfs ephemeral home was produced.
    assert any(d.startswith("/run/user/") for d in pol.secret_dirs)
    assert all(kind != "tmpfs_rw" for _, kind in pol.bind_plan())


def test_resolve_policy_defaults_disposable_tree_false(tmp_path):
    control, wt = _layout(tmp_path)
    pol = resolve_policy(
        worktree=wt, control_root=control, quest_id="abc",
        cfg=SandboxConfig(), engine_env_result={}, environ={}, available=True,
    )
    assert pol.disposable_tree is False


def test_resolve_policy_bakes_workflow_snapshot_overlay(tmp_path):
    control, wt = _layout(tmp_path)
    quest_dir = control / ".imp" / "quests" / "abc"
    (quest_dir / "workflow").mkdir(parents=True)
    pol = resolve_policy(
        worktree=wt, control_root=control, quest_id="abc",
        cfg=SandboxConfig(), engine_env_result={}, environ={},
        available=True, quest_dir=quest_dir,
    )
    assert str(quest_dir / "workflow") in pol.readonly_overlays


def test_resolve_policy_omits_workflow_overlay_when_snapshot_absent(tmp_path):
    control, wt = _layout(tmp_path)
    quest_dir = control / ".imp" / "quests" / "abc"
    quest_dir.mkdir(parents=True)   # change home exists, but no workflow/ subtree
    pol = resolve_policy(
        worktree=wt, control_root=control, quest_id="abc",
        cfg=SandboxConfig(), engine_env_result={}, environ={},
        available=True, quest_dir=quest_dir,
    )
    assert str(quest_dir / "workflow") not in pol.readonly_overlays
    assert pol.readonly_overlays == ()


def test_resolve_policy_omits_workflow_overlay_when_quest_dir_unset(tmp_path):
    control, wt = _layout(tmp_path)
    pol = resolve_policy(
        worktree=wt, control_root=control, quest_id="abc",
        cfg=SandboxConfig(), engine_env_result={}, environ={},
        available=True,   # quest_dir defaults to None
    )
    assert pol.readonly_overlays == ()


def test_resolve_policy_workflow_snapshot_denies_write_siblings_writable(tmp_path):
    control, wt = _layout(tmp_path)
    quest_dir = control / ".imp" / "quests" / "abc"
    (quest_dir / "workflow").mkdir(parents=True)
    pol = resolve_policy(
        worktree=wt, control_root=control, quest_id="abc",
        cfg=SandboxConfig(), engine_env_result={}, environ={},
        available=True, quest_dir=quest_dir,
    )
    assert pol.allows_write(quest_dir / "workflow" / "workflow.yaml") is False
    assert pol.allows_write(quest_dir / "artifacts" / "spec" / "x.md") is True


def test_resolve_policy_workflow_ro_bind_emitted_after_quest_home(tmp_path):
    control, wt = _layout(tmp_path)
    quest_dir = control / ".imp" / "quests" / "abc"
    snapshot = quest_dir / "workflow"
    snapshot.mkdir(parents=True)
    pol = resolve_policy(
        worktree=wt, control_root=control, quest_id="abc",
        cfg=SandboxConfig(), engine_env_result={}, environ={},
        available=True, quest_dir=quest_dir,
    )
    argv = " ".join(build_sandbox_argv("x", cwd=str(wt), policy=pol, engine_env={}))
    quest_home_bind = f"--bind {quest_dir} {quest_dir}"
    snapshot_bind = f"--ro-bind {snapshot} {snapshot}"
    assert quest_home_bind in argv
    assert snapshot_bind in argv
    assert argv.index(quest_home_bind) < argv.index(snapshot_bind)


def test_resolve_policy_omits_workflow_overlay_when_workflow_is_plain_file(tmp_path):
    control, wt = _layout(tmp_path)
    quest_dir = control / ".imp" / "quests" / "abc"
    quest_dir.mkdir(parents=True)
    (quest_dir / "workflow").write_text("")   # a plain file, not a directory
    pol = resolve_policy(
        worktree=wt, control_root=control, quest_id="abc",
        cfg=SandboxConfig(), engine_env_result={}, environ={},
        available=True, quest_dir=quest_dir,
    )
    assert pol.readonly_overlays == ()


def test_resolve_policy_archive_hidden_by_imp_tmpfs_not_enumerated(tmp_path):
    control, wt = _layout(tmp_path)
    archive = control / ".imp" / "archive"
    (archive / "blobs").mkdir(parents=True)
    (archive / "index.db").write_text("")
    (archive / "records").mkdir()
    pol = resolve_policy(
        worktree=wt, control_root=control, quest_id="abc",
        cfg=SandboxConfig(), engine_env_result={}, environ={}, available=True,
    )
    # No archive path is enumerated into the secret masks anymore...
    assert str(archive / "blobs") not in pol.secret_dirs
    assert str(archive / "index.db") not in pol.secret_files
    assert not any(b.startswith(str(archive)) for b, _ in pol.bind_plan())
    # ...but the whole-.imp tmpfs denies every archive path in the read mirror,
    # including records/ (a capability nothing used).
    assert pol.allows_read(archive / "blobs" / "ab" / "cd") is False
    assert pol.allows_read(archive / "index.db") is False
    assert pol.allows_read(archive / "records" / "x.json") is False


def test_resolve_policy_sibling_appearance_and_removal_are_noop(tmp_path):
    # AC 7: a sibling quest appearing or vanishing after resolve_policy changes
    # NOTHING about the current quest's resolved policy or emitted argv — the mask
    # race is gone by construction (no sibling enumeration).
    control, wt = _layout(tmp_path)
    quest_dir = control / ".imp" / "quests" / "abc"
    (quest_dir / "steps").mkdir(parents=True)
    (quest_dir / "artifacts").mkdir()
    (quest_dir / "events.jsonl").write_text("")
    # Pre-create refs/heads so resolve_policy is idempotent across calls
    # (imp_refs.mkdir creates it as a side-effect on the first call otherwise).
    (control / ".git" / "refs" / "heads").mkdir(parents=True)

    def _resolve():
        return resolve_policy(
            worktree=wt, control_root=control, quest_id="abc",
            cfg=SandboxConfig(), engine_env_result={}, environ={},
            available=True, quest_dir=quest_dir,
        )

    pol1 = _resolve()
    argv1 = build_sandbox_argv("x", cwd=str(wt), policy=pol1, engine_env={})

    # Sibling APPEARS with its own steps/ + events.jsonl.
    sib = control / ".imp" / "quests" / "other"
    (sib / "steps").mkdir(parents=True)
    (sib / "events.jsonl").write_text('{"e":1}\n')
    pol2 = _resolve()
    argv2 = build_sandbox_argv("x", cwd=str(wt), policy=pol2, engine_env={})
    assert pol2 == pol1
    assert argv2 == argv1

    # Sibling VANISHES.
    import shutil as _shutil
    _shutil.rmtree(sib)
    pol3 = _resolve()
    assert pol3 == pol1


def test_resolve_policy_sets_control_imp_tmpfs(tmp_path):
    control, wt = _layout(tmp_path)
    pol = resolve_policy(
        worktree=wt, control_root=control, quest_id="abc",
        cfg=SandboxConfig(), engine_env_result={}, environ={}, available=True,
    )
    assert pol.control_imp == str(control / ".imp")
    assert (str(control / ".imp"), "mask_dir") in pol.bind_plan()


def test_bind_plan_imp_tmpfs_before_worktree_for_nested_worktree():
    # Normal quest: worktree nested at .imp/worktrees/abc, quest home at
    # .imp/quests/abc. The tmpfs must precede the worktree rw bind, and both the
    # worktree and quest_home rebinds must follow it (last-wins re-exposes them).
    p = _policy(control_imp="/ctl/.imp", quest_home="/ctl/.imp/quests/abc")
    paths = [b for b, _ in p.bind_plan()]
    i_tmpfs = paths.index("/ctl/.imp")
    i_wt = paths.index("/ctl/.imp/worktrees/abc")
    i_home = paths.index("/ctl/.imp/quests/abc")
    assert i_tmpfs < i_wt < i_home
    assert p.bind_plan()[i_tmpfs][1] == "mask_dir"


def test_bind_plan_imp_tmpfs_after_worktree_for_draft():
    # Draft/degenerate agent: worktree == control_root (an ANCESTOR of .imp). The
    # control-root rw bind must precede the tmpfs (so the tmpfs re-masks .imp), the
    # quest_home rebind must follow it, and the worktree is NOT re-exposed after it.
    p = _policy(control_imp="/ctl/.imp", worktree="/ctl",
                quest_home="/ctl/.imp/quests/abc")
    paths = [b for b, _ in p.bind_plan()]
    i_wt = paths.index("/ctl")
    i_tmpfs = paths.index("/ctl/.imp")
    i_home = paths.index("/ctl/.imp/quests/abc")
    assert i_wt < i_tmpfs < i_home
    assert paths.count("/ctl") == 1   # worktree bound once, before the tmpfs only


def test_argv_binds_match_bind_plan_with_imp_tmpfs():
    # Drift guard: the new tmpfs round-trips through the argv parser for BOTH the
    # nested and draft orderings.
    p = _policy(control_imp="/ctl/.imp", quest_home="/ctl/.imp/quests/abc")
    argv = build_sandbox_argv("x", cwd="/wt", policy=p, engine_env={})
    assert _parse_argv_binds(argv) == p.bind_plan()
    assert ("/ctl/.imp", "mask_dir") in p.bind_plan()

    pd = _policy(control_imp="/ctl/.imp", worktree="/ctl",
                 quest_home="/ctl/.imp/quests/abc")
    argvd = build_sandbox_argv("x", cwd="/ctl", policy=pd, engine_env={})
    assert _parse_argv_binds(argvd) == pd.bind_plan()


def test_allows_read_draft_tmpfs_supersedes_control_root_bind():
    # AC 5 / AC 9(b): in the draft case the control-root rw bind covers a sibling
    # path, but the LATER .imp tmpfs supersedes it — siblings, archive, and config
    # read as denied, while the re-exposed own home reads True and its own
    # steps/events masks still deny.
    p = _policy(
        control_imp="/ctl/.imp", worktree="/ctl",
        quest_home="/ctl/.imp/quests/abc",
        secret_dirs=("/ctl/.imp/quests/abc/steps", "/run/user/1000"),
        secret_files=("/ctl/.imp/quests/abc/events.jsonl",),
    )
    assert p.allows_read(Path("/ctl/.imp/quests/other/events.jsonl")) is False
    assert p.allows_read(Path("/ctl/.imp/archive/blobs/ab/cd")) is False
    assert p.allows_read(Path("/ctl/.imp/config.yaml")) is False
    assert p.allows_read(Path("/ctl/.imp/quests/abc/artifacts/spec/abc.md")) is True
    assert p.allows_read(Path("/ctl/.imp/quests/abc/steps/x")) is False
    assert p.allows_read(Path("/ctl/.imp/quests/abc/events.jsonl")) is False
    assert p.allows_write(Path("/ctl/.imp/quests/other/x")) is False
    assert p.allows_write(Path("/ctl/.imp/quests/abc/artifacts/x.md")) is True


def test_allows_read_nested_tmpfs_masks_siblings_and_archive():
    # Nested (normal) worktree: the .imp tmpfs precedes the worktree rw bind. A
    # sibling home and the archive read as denied; the re-exposed own home and the
    # nested worktree read True, while the own steps/events masks still deny.
    p = _policy(
        control_imp="/ctl/.imp", worktree="/ctl/.imp/worktrees/abc",
        quest_home="/ctl/.imp/quests/abc",
        secret_dirs=("/ctl/.imp/quests/abc/steps", "/run/user/1000"),
        secret_files=("/ctl/.imp/quests/abc/events.jsonl",),
    )
    assert p.allows_read(Path("/ctl/.imp/quests/other/events.jsonl")) is False
    assert p.allows_read(Path("/ctl/.imp/archive/blobs/ab/cd")) is False
    assert p.allows_read(Path("/ctl/.imp/config.yaml")) is False
    assert p.allows_read(Path("/ctl/.imp/quests/abc/artifacts/spec/abc.md")) is True
    assert p.allows_read(Path("/ctl/.imp/worktrees/abc/src/x.py")) is True
    assert p.allows_read(Path("/ctl/.imp/quests/abc/steps/x")) is False
    assert p.allows_read(Path("/ctl/.imp/quests/abc/events.jsonl")) is False


def test_resolve_policy_injects_session_socket_marker(tmp_path):
    from imp.remote.session.discover import session_socket_path
    control, wt = _layout(tmp_path)
    pol = resolve_policy(
        worktree=wt, control_root=control, quest_id="abc",
        cfg=SandboxConfig(), engine_env_result={}, environ={"PATH": "/usr/bin"},
        available=True,
    )
    # No rewriting: the in-sandbox value is the real host socket path.
    assert ("IMP_SESSION_SOCKET",
            str(session_socket_path(control, "abc"))) in pol.setenv


def test_session_socket_marker_absent_when_inactive(tmp_path):
    control, wt = _layout(tmp_path)
    pol = resolve_policy(
        worktree=wt, control_root=control, quest_id="abc",
        cfg=SandboxConfig(enabled=False), engine_env_result={},
        environ={}, available=True,
    )
    assert "IMP_SESSION_SOCKET" not in {k for k, _ in pol.setenv}

    pol = resolve_policy(
        worktree=wt, control_root=control, quest_id="abc",
        cfg=SandboxConfig(allow_unsandboxed=True), engine_env_result={},
        environ={}, available=False,
    )
    assert "IMP_SESSION_SOCKET" not in {k for k, _ in pol.setenv}


def test_session_socket_marker_reaches_bwrap_setenv(tmp_path):
    control, wt = _layout(tmp_path)
    pol = resolve_policy(
        worktree=wt, control_root=control, quest_id="abc",
        cfg=SandboxConfig(), engine_env_result={}, environ={"PATH": "/usr/bin"},
        available=True,
    )
    argv = build_sandbox_argv("x", cwd=str(wt), policy=pol, engine_env={})
    from imp.remote.session.discover import session_socket_path
    i = argv.index("IMP_SESSION_SOCKET")
    assert argv[i - 1] == "--setenv"
    assert argv[i + 1] == str(session_socket_path(control, "abc"))


def test_build_setenv_prepends_shim_dir_to_path():
    import os

    from imp.engine.sandbox import _build_setenv
    setenv = dict(_build_setenv({}, {"PATH": "/usr/bin"}, (), shim_dir="/q/imp-shim"))
    assert setenv["PATH"] == "/q/imp-shim" + os.pathsep + "/usr/bin"


def test_build_setenv_no_shim_dir_leaves_path_untouched():
    from imp.engine.sandbox import _build_setenv
    setenv = dict(_build_setenv({}, {"PATH": "/usr/bin"}, ()))
    assert setenv["PATH"] == "/usr/bin"


def test_build_setenv_prepends_shim_dir_no_existing_path():
    from imp.engine.sandbox import _build_setenv
    setenv = dict(_build_setenv({}, {}, (), shim_dir="/q/imp-shim"))
    assert setenv["PATH"] == "/q/imp-shim"


def test_resolve_policy_prepends_shim_dir_when_quest_dir_given(tmp_path):
    import os
    control, wt = _layout(tmp_path)
    qd = control / ".imp" / "quests" / "abc"
    pol = resolve_policy(
        worktree=wt, control_root=control, quest_id="abc", cfg=SandboxConfig(),
        engine_env_result={}, environ={"PATH": "/usr/bin"}, available=True,
        quest_dir=qd,
    )
    path = dict(pol.setenv)["PATH"]
    assert path.startswith(str(qd / "imp-shim") + os.pathsep)
    assert path.endswith("/usr/bin")


def test_worktree_swap_flips_exposure_to_new_worktree(tmp_path):
    # Go-live refreshes the policy with a single dataclasses.replace of `worktree`.
    # Swapping the draft's control-root worktree for the nested .imp/worktrees/<id>
    # path must flip exposure from draft-shaped to live-shaped.
    control, wt = _layout(tmp_path)                 # wt = control/.imp/worktrees/abc
    quest_dir = control / ".imp" / "quests" / "abc"
    quest_dir.mkdir(parents=True)
    draft = resolve_policy(
        worktree=control, control_root=control, quest_id="abc",
        cfg=SandboxConfig(), engine_env_result={}, environ={},
        available=True, quest_dir=quest_dir)
    live = dataclasses.replace(draft, worktree=str(wt))

    wt_file = wt / "src" / "app.py"
    # (a) a path inside the new worktree: denied under draft, allowed under live.
    assert draft.allows_read(wt_file) is False
    assert draft.allows_write(wt_file) is False
    assert live.allows_read(wt_file) is True
    assert live.allows_write(wt_file) is True
    # (b) the quest home stays allowed after the swap.
    assert live.allows_read(quest_dir / "artifacts" / "spec.md") is True
    assert live.allows_write(quest_dir / "artifacts" / "spec.md") is True
    # (c) a sibling elsewhere under .imp stays masked.
    sibling = control / ".imp" / "worktrees" / "other" / "f.py"
    assert live.allows_read(sibling) is False


def test_build_setenv_session_socket_passes_through():
    from imp.engine.sandbox import _build_setenv
    setenv = dict(_build_setenv(
        {}, {"PATH": "/usr/bin"}, (),
        session_socket="/ctl/.imp/quests/abc/session.sock"))
    assert setenv["IMP_SESSION_SOCKET"] == "/ctl/.imp/quests/abc/session.sock"


def test_imp_output_env_passes_through_unrewritten():
    pol = _policy()
    flat = " ".join(build_sandbox_argv(
        "x", cwd="/wt", policy=pol,
        engine_env={"IMP_OUTPUT": "/t/imp-1000/abc/imp-output-XY.json",
                    "IMP_PROJECT_ROOT": "/ctl/.imp/worktrees/abc"}))
    assert "--setenv IMP_OUTPUT /t/imp-1000/abc/imp-output-XY.json" in flat
    assert "--setenv IMP_PROJECT_ROOT /ctl/.imp/worktrees/abc" in flat


def test_var_tmp_bind_source_and_position():
    # Set case: the per-quest source is bound onto /var/tmp, positioned in
    # bind_plan immediately AFTER the /tmp scratch entry and before the worktree.
    pol = _policy(quest_var_tmp_dir="/vt/imp-1000/abc")
    flat = " ".join(build_sandbox_argv("x", cwd="/wt", policy=pol, engine_env={}))
    assert "--bind /vt/imp-1000/abc /var/tmp" in flat   # per-quest source → /var/tmp
    argv = build_sandbox_argv("x", cwd="/wt", policy=pol, engine_env={})
    i_tmp = argv.index("/tmp")                    # first "/tmp" is the /tmp bind source
    i_vt = argv.index("/vt/imp-1000/abc")         # the /var/tmp bind source
    i_wt = argv.index("/ctl/.imp/worktrees/abc")  # worktree src (default _policy worktree)
    assert i_tmp < i_vt < i_wt
    bp = pol.bind_plan()
    assert bp[bp.index(("/tmp", "rw")) + 1] == ("/var/tmp", "rw")  # right after /tmp
    assert pol.allows_write(Path("/var/tmp/scratch")) is True


def test_no_var_tmp_bind_when_unset():
    # Negative: empty quest_var_tmp_dir emits NO /var/tmp bind — host /var/tmp
    # stays on the read-only floor (no identity fallback). Drift guard still holds.
    pol = _policy()   # quest_var_tmp_dir defaults to ""
    argv = build_sandbox_argv("x", cwd="/wt", policy=pol, engine_env={})
    assert "/var/tmp" not in argv                       # no bind dest emitted at all
    assert ("/var/tmp", "rw") not in pol.bind_plan()
    assert pol.allows_write(Path("/var/tmp/x")) is False # uncovered → read-only floor
    assert pol.allows_read(Path("/var/tmp/x")) is True   # readable via the ro floor
    assert _parse_argv_binds(argv) == pol.bind_plan()    # argv/plan agree (no drift)


def test_argv_binds_match_bind_plan_with_var_tmp_source():
    # /tmp is now an identity bind; /var/tmp still binds a per-quest host SOURCE
    # onto its namespace DEST. Parse-back is dest-keyed, so it equals bind_plan().
    pol = _policy(quest_var_tmp_dir="/vt/imp-1000/abc")
    argv = build_sandbox_argv("x", cwd="/wt", policy=pol, engine_env={})
    flat = " ".join(argv)
    assert "--bind /tmp /tmp" in flat                   # identity /tmp source
    assert "--bind /vt/imp-1000/abc /var/tmp" in flat   # non-identity /var/tmp source
    assert _parse_argv_binds(argv) == pol.bind_plan()


def test_resolve_policy_tmpdir_is_per_quest_scratch(tmp_path):
    control, wt = _layout(tmp_path)
    pol = resolve_policy(
        worktree=wt, control_root=control, quest_id="abc",
        cfg=SandboxConfig(), engine_env_result={}, environ={}, available=True,
    )
    expected = str(Path(tempfile.gettempdir()) / f"imp-{os.getuid()}" / "abc")
    assert ("TMPDIR", expected) in pol.setenv


def test_var_tmp_source_not_identity_fallback():
    # An empty quest_var_tmp_dir must NEVER degrade to --bind /var/tmp /var/tmp
    # (which would expose the host's real /var/tmp read-write).
    flat = " ".join(build_sandbox_argv(
        "x", cwd="/wt", policy=_policy(), engine_env={}))
    assert "--bind /var/tmp /var/tmp" not in flat


def test_var_tmp_mirror_keeps_var_tmp_writable_dest_keyed():
    # bind_plan stays DEST-keyed on /var/tmp (the in-sandbox path) even though argv
    # binds the per-quest host source, so allows_write reports /var/tmp writable.
    pol = _policy(quest_var_tmp_dir="/vt/imp-1000/abc")
    assert ("/var/tmp", "rw") in pol.bind_plan()
    assert pol.allows_write(Path("/var/tmp/a/b")) is True


def test_resolve_policy_sets_var_tmp_when_writable(tmp_path, monkeypatch):
    import imp.engine.sandbox as sandbox_mod
    from imp.engine.env import quest_var_tmp_root
    control, wt = _layout(tmp_path)
    qd = tmp_path / "qd"
    qd.mkdir()
    root = quest_var_tmp_root()
    real_access = os.access
    monkeypatch.setattr(
        sandbox_mod.os, "access",
        lambda p, m: True if str(p) == str(root) else real_access(p, m))
    pol = resolve_policy(
        worktree=wt, control_root=control, quest_id="abc",
        cfg=SandboxConfig(uid=99999),        # injected uid must be IGNORED for /var/tmp
        engine_env_result={}, environ={}, available=True, quest_dir=qd)
    # uid comes from os.getuid() unconditionally, NOT cfg.uid=99999
    assert pol.quest_var_tmp_dir == f"{root}/imp-{os.getuid()}/abc"
    assert "imp-99999" not in pol.quest_var_tmp_dir
    assert ("/var/tmp", "rw") in pol.bind_plan()   # bind emitted (namespace dest, always /var/tmp)


def test_resolve_policy_no_var_tmp_when_unwritable(tmp_path, monkeypatch):
    import imp.engine.sandbox as sandbox_mod
    from imp.engine.env import quest_var_tmp_root
    control, wt = _layout(tmp_path)
    qd = tmp_path / "qd"
    qd.mkdir()
    root = quest_var_tmp_root()
    real_access = os.access
    monkeypatch.setattr(
        sandbox_mod.os, "access",
        lambda p, m: False if str(p) == str(root) else real_access(p, m))
    pol = resolve_policy(
        worktree=wt, control_root=control, quest_id="abc",
        cfg=SandboxConfig(), engine_env_result={}, environ={},
        available=True, quest_dir=qd)
    assert pol.quest_var_tmp_dir == ""
    assert ("/var/tmp", "rw") not in pol.bind_plan()


def test_resolve_policy_no_var_tmp_for_draft_without_quest_dir(tmp_path, monkeypatch):
    import imp.engine.sandbox as sandbox_mod
    control, wt = _layout(tmp_path)
    real_access = os.access
    monkeypatch.setattr(          # /var/tmp writable, but quest_dir is None (draft)
        sandbox_mod.os, "access",
        lambda p, m: True if str(p) == "/var/tmp" else real_access(p, m))
    pol = resolve_policy(
        worktree=wt, control_root=control, quest_id="abc",
        cfg=SandboxConfig(), engine_env_result={}, environ={},
        available=True, quest_dir=None)
    assert pol.quest_var_tmp_dir == ""


def test_bind_plan_emits_worktrees_mask_then_self_after_ref_reexpose():
    # The mask (mask_dir) and the self re-expose (rw) are emitted AFTER the
    # git_ref_reexpose entry, in that order — load-bearing so last-wins hides
    # siblings then re-exposes self.
    p = _policy(
        git_ref_reexpose=("/ctl/.git/refs/heads/imp",),
        git_worktrees_mask="/ctl/.git/worktrees",
        git_worktree_self="/ctl/.git/worktrees/abc",
    )
    plan = p.bind_plan()
    i_ref = plan.index(("/ctl/.git/refs/heads/imp", "rw"))
    i_mask = plan.index(("/ctl/.git/worktrees", "mask_dir"))
    i_self = plan.index(("/ctl/.git/worktrees/abc", "rw"))
    assert i_ref < i_mask < i_self


def test_worktrees_mask_hides_siblings_reexposes_self():
    # Without the new binds, git_common_dir's rw bind would make every admin dir
    # under .git/worktrees writable. The mask hides siblings; the self re-expose
    # keeps self's admin dir writable.
    p = _policy(
        git_worktrees_mask="/ctl/.git/worktrees",
        git_worktree_self="/ctl/.git/worktrees/abc",
    )
    assert p.allows_write(Path("/ctl/.git/worktrees/abc/HEAD")) is True
    assert p.allows_write(Path("/ctl/.git/worktrees/sib/HEAD")) is False
    assert p.allows_read(Path("/ctl/.git/worktrees/sib/gitdir")) is False


def test_worktrees_mask_only_masks_self_too():
    # Draft-concierge shape: mask set, self empty → the whole worktrees tree is
    # hidden (no re-expose), including the would-be self admin dir.
    p = _policy(git_worktrees_mask="/ctl/.git/worktrees", git_worktree_self="")
    assert p.allows_read(Path("/ctl/.git/worktrees/abc/HEAD")) is False
    assert p.allows_write(Path("/ctl/.git/worktrees/abc/HEAD")) is False


def test_argv_binds_match_bind_plan_with_worktrees_mask():
    # Drift guard: the new mask_dir + rw binds round-trip through the argv parser,
    # so build_sandbox_argv picks them up with no edit to build_sandbox_argv.
    p = _policy(
        git_worktrees_mask="/ctl/.git/worktrees",
        git_worktree_self="/ctl/.git/worktrees/abc",
    )
    argv = build_sandbox_argv("x", cwd="/wt", policy=p, engine_env={})
    assert _parse_argv_binds(argv) == p.bind_plan()
    assert ("/ctl/.git/worktrees", "mask_dir") in p.bind_plan()
    assert ("/ctl/.git/worktrees/abc", "rw") in p.bind_plan()
