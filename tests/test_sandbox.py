"""Always-on sandbox tests — no bwrap needed (argv, policy resolution, and the
wrapper's own shell semantics under a plain host shell)."""
from __future__ import annotations

import dataclasses
import os
import subprocess
import tempfile
import types as _types
import uuid
from pathlib import Path

import pytest

import imp.engine.sandbox as sandbox_mod
from imp.engine.env import engine_env
from imp.engine.sandbox import (
    KEEPER_PAYLOAD,
    SANDBOX_SPAWN_MAX_ATTEMPTS,
    HomeLayer,
    OverlayRoot,
    SandboxConfig,
    SandboxNamespace,
    SandboxOpenFailed,
    SandboxPolicy,
    SandboxUnavailable,
    _force_rmtree,
    build_sandbox_spawn,
    make_home_layer,
    prepare_spawn_policy,
    resolve_policy,
    sandbox_available,
    sandbox_config_from_raw,
)
from imp.paths import imp_home, project_state_dir
from imp.providers.config import parse_config_layer


def _policy(**over) -> SandboxPolicy:
    base = dict(
        active=True,
        worktree="/state/worktrees/abc",
        git_common_dir="/ctl/.git",
        git_ro_remask=("/ctl/.git/hooks", "/ctl/.git/config"),
        writable_caches=("/home/u/.cache", "/home/u/.cargo"),
        extra_readable=(),
        extra_writable=(),
        secret_dirs=("/home/u/.ssh", "/run/user/1000"),
        secret_files=("/home/u/.netrc",),
        child_env=(("PATH", "/usr/bin"), ("HOME", "/home/u"),
                ("IMP_PROJECT_ROOT", "/state/worktrees/abc"),
                ("TMPDIR", "/tmp/imp-1000/abc")),
        network=True,
        note="",
    )
    base.update(over)
    return SandboxPolicy(**base)


def _argv(command: str, **kw) -> list[str]:
    """The spawn argv alone, for the pure argv-shape tests (the child env is
    asserted separately, by the tests that own it)."""
    return build_sandbox_spawn(command, environ={}, **kw).argv


def wrapper_argv(command: str) -> list[str]:
    """The trailing `sh -c <sentinel script> <command>` argv build_sandbox_spawn
    emits, with the bwrap prefix sliced off — lets a test run the REAL wrapper
    text on the host, so wrapper semantics are pinned without needing bwrap."""
    return build_sandbox_spawn(
        command, cwd="/", policy=_policy(), engine_env={}, environ={}).argv[-4:]


def test_inactive_policy_returns_bare_argv():
    from imp.engine.sandbox import SANDBOX_ENTERED_MARKER
    spawn = build_sandbox_spawn("echo hi", cwd="/state/worktrees/abc",
                                policy=_policy(active=False), engine_env={}, environ={})
    assert spawn.argv[:2] == ["sh", "-c"]
    assert "echo hi" not in spawn.argv[2]          # the command is not on argv
    assert spawn.argv[2].splitlines() == [
        "cmd=$IMP_COMMAND", "unset IMP_COMMAND", 'eval "$cmd"']
    assert spawn.env["IMP_COMMAND"] == "echo hi"
    assert SANDBOX_ENTERED_MARKER not in spawn.argv[2]


def test_active_policy_wraps_with_bwrap():
    argv = _argv("echo hi", cwd="/state/worktrees/abc",
                              policy=_policy(), engine_env={})
    assert argv[0] == "bwrap"
    assert argv[-4:-2] == ["sh", "-c"]
    assert argv[-1] == "echo hi"                  # command is its own argv element
    assert "echo hi" not in argv[-2]              # never interpolated into the script
    assert argv[1:4] == ["--ro-bind", "/", "/"]   # ro-floor first


def test_active_policy_emits_entry_sentinel_in_tail():
    import shlex

    from imp.engine.sandbox import SANDBOX_ENTERED_MARKER
    argv = _argv("echo hi", cwd="/state/worktrees/abc",
                              policy=_policy(), engine_env={})
    assert argv[-4:-2] == ["sh", "-c"]
    expected_script = (
        f"printf '%s\\n' {shlex.quote(SANDBOX_ENTERED_MARKER)} >&2; "
        'exec sh -c "$0"')
    assert argv[-2] == expected_script
    assert argv[-1] == "echo hi"


def _run_wrapper(command: str, stdin: str | None = None) -> subprocess.CompletedProcess[str]:
    """Run the builder's real wrapper argv on the host (no bwrap needed)."""
    return subprocess.run(wrapper_argv(command), input=stdin, text=True,
                          capture_output=True)


def _run_bare(command: str, stdin: str | None = None) -> subprocess.CompletedProcess[str]:
    """Run the builder's real UNSANDBOXED spawn on the host — the `/sandbox off`
    shape, whose command travels in the environment exactly as a join's does."""
    spawn = build_sandbox_spawn(
        command, cwd="/", policy=_policy(active=False), engine_env={},
        environ={"PATH": os.environ.get("PATH", "/usr/bin:/bin")})
    return subprocess.run(spawn.argv, input=stdin, text=True, capture_output=True,
                          env=spawn.env)


def test_wrapper_emits_the_sentinel_before_a_syntax_error():
    # AC 1/7a: the outer shell's printf is not in the command's parse unit, so an
    # unterminated backquote exits 2 AFTER the sentinel exists.
    from imp.engine.sandbox import SANDBOX_ENTERED_MARKER
    p = _run_wrapper('echo "`')
    assert p.returncode == 2
    lines = [ln for ln in p.stderr.splitlines() if ln.strip()]
    assert lines[0] == SANDBOX_ENTERED_MARKER
    assert len(lines) > 1                 # the shell's own diagnosis follows it


def test_wrapper_preserves_set_e():
    # AC 8a: `false` under set -e still aborts before the next command.
    p = _run_wrapper("set -e; echo hello; false; echo unreachable")
    assert p.returncode == 1
    assert p.stdout == "hello\n"


def test_wrapper_passes_the_exit_code_through():
    # AC 8b: the inner shell is exec'd, so its status is the wrapper's status.
    assert _run_wrapper("exit 7").returncode == 7


def test_wrapper_pipes_stdin_to_the_command():
    # AC 8c: exec keeps fd 0 attached to the command.
    p = _run_wrapper("cat", stdin="piped-in\n")
    assert p.returncode == 0
    assert p.stdout == "piped-in\n"


def test_wrapper_survives_quotes_backslashes_and_dollars():
    # AC 8d: behaviour unchanged for quotes, backslashes and `$` — expansion
    # happens exactly where the command asked for it, and nowhere else.
    cmd = r"""printf '%s\n' "dq $((1+1))" 'sq' 'lit$eral' 'back\slash'"""
    p = _run_wrapper(cmd)
    assert p.returncode == 0
    assert p.stdout.splitlines() == ["dq 2", "sq", "lit$eral", r"back\slash"]


def test_the_bare_shape_hides_the_command_from_its_own_pkill():
    # AC 10b: /sandbox off keeps the footgun unless the command leaves argv. The
    # needle occurs only inside the command's own text, so a correct build matches
    # nothing; with the command on argv, pkill kills the shell running it (rc -15).
    needle = f"imp-needle-{uuid.uuid4().hex}"
    done = _run_bare(f'pkill -f "{needle}"; echo pkill-rc=$?; ps auxww')
    assert done.returncode == 0
    assert done.stdout.splitlines()[0] == "pkill-rc=1"   # pkill: no process matched
    # Sampled host-wide from inside, WHILE the shell running the command is alive:
    # a sample taken after it exits cannot tell "never on argv" from "gone".
    assert needle not in done.stdout


def test_the_bare_shape_matches_a_plain_sh_c():
    # AC 11a/b: status propagation, set -e, $0/$#/$@ and the command's own stdin
    # (which `sh -s` would have eaten — that is why it was rejected) are exactly
    # what `sh -c <command>` gives today. Only that half of the stdin claim is
    # pinned here: "/dev/null when nothing is piped" is the SPAWN's doing
    # (_spawn_to_files' stdin=DEVNULL), which this shape change does not touch.
    assert _run_bare("exit 7").returncode == 7
    assert _run_bare("true; false").returncode == 1
    aborted = _run_bare("set -e; echo hello; false; echo unreachable")
    assert (aborted.returncode, aborted.stdout) == (1, "hello\n")
    assert _run_bare('echo "[$0] [$#] [$@]"').stdout == "[sh] [0] []\n"
    piped = _run_bare("cat", stdin="piped-in\n")
    assert (piped.returncode, piped.stdout) == (0, "piped-in\n")
    quoted = _run_bare(r"""printf '%s\n' "dq $((1+1))" 'sq' 'lit$eral' 'back\slash'""")
    assert quoted.stdout.splitlines() == ["dq 2", "sq", "lit$eral", r"back\slash"]
    # `eval $cmd` unquoted would word-split the value and rejoin on single
    # spaces, collapsing the run inside the quoted argument.
    spaced = _run_bare(r"""printf '%s\n' 'two  spaces'""")
    assert spaced.stdout == "two  spaces\n"


def test_the_bare_shape_leaves_no_imp_command_in_a_childs_environment():
    # AC 11c: the unset keeps the child environment byte-identical to today's. This
    # is NOT secrecy — /proc/<pid>/environ and `ps e` still show the command, since
    # they read the exec-time block the unset does not rewrite.
    assert "IMP_COMMAND" not in _run_bare("env").stdout
    assert "IMP_COMMAND" not in _run_bare("sh -c env").stdout


def test_worktree_and_git_are_rw_bound_at_real_path():
    argv = " ".join(_argv("x", cwd="/wt", policy=_policy(), engine_env={}))
    assert "--bind /state/worktrees/abc /state/worktrees/abc" in argv
    assert "--bind /ctl/.git /ctl/.git" in argv


def test_git_escape_vectors_are_remasked_ro():
    argv = " ".join(_argv("x", cwd="/wt", policy=_policy(), engine_env={}))
    assert "--ro-bind /ctl/.git/hooks /ctl/.git/hooks" in argv
    assert "--ro-bind /ctl/.git/config /ctl/.git/config" in argv


def test_prepare_spawn_policy_drops_vanished_sibling_paths(tmp_path):
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
    pruned = prepare_spawn_policy(p)

    assert pruned.secret_dirs == (str(live_steps),)
    assert pruned.secret_files == (str(live_events),)
    assert pruned.git_ro_remask == (str(live_cfg),)
    # The resulting argv references none of the vanished paths.
    argv = " ".join(_argv("x", cwd="/wt", policy=pruned, engine_env={}))
    for gone in (gone_steps, gone_events, gone_cfg):
        assert str(gone) not in argv
    # Live paths still masked.
    assert str(live_steps) in argv
    assert str(live_events) in argv


def test_prepare_spawn_policy_recreates_vanished_ref_reexpose_dir(tmp_path):
    # git prunes a ref directory once its last loose ref is packed away, so
    # .git/refs/heads/imp can vanish AFTER the policy froze it as a bind source —
    # bwrap then dies in setup on every later spawn. The dir is imp's own, so the
    # spawn-time fix is to re-create it, not to drop the bind (dropping it would
    # leave refs/heads read-only and break commits on the quest branch).
    imp_refs = tmp_path / ".git" / "refs" / "heads" / "imp"
    imp_refs.mkdir(parents=True)      # created at policy-bake time
    imp_refs.rmdir()                  # ...then pruned mid-run
    p = _policy(git_ref_reexpose=(str(imp_refs),))

    prepared = prepare_spawn_policy(p)

    assert imp_refs.is_dir()
    assert prepared.git_ref_reexpose == (str(imp_refs),)
    argv = " ".join(_argv("x", cwd="/wt", policy=prepared, engine_env={}))
    assert f"--bind {imp_refs} {imp_refs}" in argv


def test_prepare_spawn_policy_noop_on_inactive_policy(tmp_path):
    never = tmp_path / ".git" / "refs" / "heads" / "imp"
    p = _policy(active=False, git_ref_reexpose=(str(never),))
    assert prepare_spawn_policy(p) is p
    assert not never.exists()


def test_secret_masks_emitted_after_every_bind_even_overlapping_extra_writable():
    # L1-r5: a mask placed BEFORE an overlapping extra_writable would be clobbered.
    p = _policy(extra_writable=("/home/u",), secret_dirs=("/home/u/.ssh",))
    flat = _argv("x", cwd="/wt", policy=p, engine_env={})
    i_writable = flat.index("/home/u")                      # the extra_writable bind dest
    i_tmpfs = flat.index("--tmpfs")
    assert i_tmpfs > i_writable, "secret --tmpfs must come AFTER extra_writable bind"


def test_real_tmp_is_bound_rw_early():
    argv = _argv("x", cwd="/wt", policy=_policy(), engine_env={})
    flat = " ".join(argv)
    # Real /tmp is bound rw (identity bind).
    assert "--bind /tmp /tmp" in flat
    # Ordering: /tmp bind must appear right after the ro-floor and BEFORE
    # the worktree bind (bwrap last-wins; /tmp early means worktree/git/remask
    # still win on their paths even if a repo lives under /tmp).
    i_ro_floor = argv.index("/")          # first "/" after --ro-bind is the src
    i_tmp_bind = argv.index("/tmp")       # first "/tmp" after --bind is the src
    i_wt_bind = argv.index("/state/worktrees/abc")  # worktree src
    # The load-bearing invariant: /tmp must be bound BEFORE the git RO-remask, so a
    # repo whose .git lives under /tmp still has hooks/config remasked RO (the late
    # remask wins over the early /tmp bind). Assert directly against the remask, not
    # just transitively via the worktree bind.
    i_first_remask = argv.index("/ctl/.git/hooks")
    assert i_tmp_bind > i_ro_floor, "/tmp bind must come after ro-floor"
    assert i_tmp_bind < i_wt_bind, "/tmp bind must come BEFORE worktree bind"
    assert i_tmp_bind < i_first_remask, "/tmp bind must come BEFORE the git RO-remask"


def test_child_env_carries_the_allowlist():
    spawn = build_sandbox_spawn("x", cwd="/wt", policy=_policy(), engine_env={},
                                environ={})
    assert spawn.env["IMP_PROJECT_ROOT"] == "/state/worktrees/abc"
    assert spawn.env["TMPDIR"] == "/tmp/imp-1000/abc"


def test_live_engine_env_supersedes_frozen_imp_vars():
    # Regression (name-on-intent + sandbox): a Quest is created with an empty
    # title, so the policy is frozen with IMP_QUEST_TITLE="" at run start. After
    # rename_quest the live per-spawn engine_env carries the real title;
    # build_sandbox_spawn sources IMP_* from it authoritatively, so a title-dependent
    # gate never sees the frozen "".
    pol = _policy(child_env=(("PATH", "/usr/bin"),
                             ("IMP_QUEST_TITLE", ""),
                             ("TMPDIR", "/tmp")))
    spawn = build_sandbox_spawn(
        "x", cwd="/wt", policy=pol,
        engine_env={"IMP_QUEST_TITLE": "Live mode log polish",
                    "PYTHONPATH": "/wt/src"}, environ={})
    assert spawn.env["IMP_QUEST_TITLE"] == "Live mode log polish"   # fresh value wins
    assert "Live mode log polish" not in " ".join(spawn.argv)
    # the single deliberate non-IMP_ passthrough
    assert spawn.env["PYTHONPATH"] == "/wt/src"


def test_new_imp_var_flows_through_without_code_change():
    # Future-proofing: the IMP_ prefix is the allowlist, so a brand-new engine var
    # reaches the sandbox with no edit to build_sandbox_spawn.
    spawn = build_sandbox_spawn(
        "x", cwd="/wt", policy=_policy(),
        engine_env={"IMP_FUTURE_VAR": "v", "SOME_OTHER": "no"}, environ={})
    assert spawn.env["IMP_FUTURE_VAR"] == "v"
    assert "SOME_OTHER" not in spawn.env


def test_pythonpath_flows_through_build_sandbox_spawn():
    # PYTHONPATH is the single deliberate non-IMP_ key the allowlist passes
    # through (the imp-on-imp import shadow).
    spawn = build_sandbox_spawn(
        "x", cwd="/wt", policy=_policy(),
        engine_env={"PYTHONPATH": "/state/worktrees/abc/src"}, environ={})
    assert spawn.env["PYTHONPATH"] == "/state/worktrees/abc/src"


def test_no_pythonpath_in_child_env_when_engine_env_lacks_it():
    # A non-imp project's engine_env carries no PYTHONPATH, so none is delivered.
    spawn = build_sandbox_spawn("x", cwd="/wt", policy=_policy(), engine_env={},
                                environ={})
    assert "PYTHONPATH" not in spawn.env


def test_requires_pid_namespace_and_no_die_with_parent():
    argv = _argv("x", cwd="/wt", policy=_policy(), engine_env={})
    assert "--unshare-pid" in argv          # process-table isolation
    # B.9.2 (H-A): a PER-CALL spawn never carries it — it would collapse the
    # runner's SIGTERM grace. Only the keeper does (see the keeper tests below).
    assert "--die-with-parent" not in argv
    assert "--new-session" not in argv      # B.9.3 (F-8)
    assert "--unshare-user" in argv


def test_keeper_spawn_carries_info_fd_and_die_with_parent():
    spawn = build_sandbox_spawn(
        KEEPER_PAYLOAD, cwd="/wt", policy=_policy(), engine_env={}, environ={},
        keeper_info_fd=7)
    assert spawn.argv[spawn.argv.index("--info-fd") + 1] == "7"
    assert "--die-with-parent" in spawn.argv   # dead-man's switch, KEEPER only
    assert spawn.argv[-1] == KEEPER_PAYLOAD
    # the flag and the fd the child must inherit travel together
    assert spawn.pass_fds == (7,)


def test_keeper_flags_survive_the_overlay_unshare_wrapper():
    # The overlay shape embeds the whole bwrap argv in an `sh -c` string, so the
    # keeper flags have to be emitted BEFORE the wrap, not spliced in after it.
    pol = _policy(home="/home/u", home_layer=HomeLayer(
        home="/home/u", merged="/ovl/merged", upper="/ovl/upper", work="/ovl/work"))
    spawn = build_sandbox_spawn(
        KEEPER_PAYLOAD, cwd="/wt", policy=pol, engine_env={}, environ={},
        keeper_info_fd=9)
    assert spawn.argv[0] == "unshare"
    assert "--info-fd 9 --die-with-parent" in spawn.argv[-1]
    assert spawn.pass_fds == (9,)


def test_an_ordinary_spawn_passes_no_fds():
    spawn = build_sandbox_spawn("x", cwd="/wt", policy=_policy(), engine_env={},
                                environ={})
    assert spawn.pass_fds == ()


def test_network_off_adds_unshare_net():
    on = _argv("x", cwd="/wt", policy=_policy(network=True), engine_env={})
    off = _argv("x", cwd="/wt", policy=_policy(network=False), engine_env={})
    assert "--unshare-net" not in on
    assert "--unshare-net" in off


def test_chdir_is_cwd():
    argv = " ".join(_argv("x", cwd="/state/worktrees/abc/sub",
                                       policy=_policy(), engine_env={}))
    assert "--chdir /state/worktrees/abc/sub" in argv


def _layout(tmp_path):
    control = tmp_path / "ctl"
    control.mkdir()
    wt = project_state_dir(control) / "worktrees" / "abc"
    wt.mkdir(parents=True)
    (control / ".git" / "hooks").mkdir(parents=True)
    (control / ".git" / "config").write_text("")
    wdir = control / ".git" / "worktrees" / "abc"
    wdir.mkdir(parents=True)
    (wdir / "config.worktree").write_text("")
    return control, wt.resolve()


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
    assert ("IMP_PROJECT_ROOT", str(wt)) in pol.child_env
    assert ("TMPDIR",
            str(Path(tempfile.gettempdir()) / f"imp-{os.getuid()}" / "abc")) in pol.child_env


def test_resolve_policy_masks_worktrees_not_sibling_config(tmp_path):
    control, wt = _layout(tmp_path)               # wt basename == "abc"
    sib = control / ".git" / "worktrees" / "sib"
    sib.mkdir(parents=True)
    (sib / "config.worktree").write_text("")
    pol = resolve_policy(worktree=wt, control_root=control, quest_id="abc",
                         cfg=SandboxConfig(), engine_env_result={}, environ={},
                         available=True,
                         quest_dir=project_state_dir(control) / "quests" / "abc")
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
                         quest_dir=project_state_dir(control) / "quests" / "abc")
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
                         quest_dir=project_state_dir(control) / "quests" / "abc")
    git = control / ".git"
    assert pol.git_worktrees_mask == str(git / "worktrees")
    assert pol.git_worktree_self == ""
    assert pol.allows_read(git / "worktrees" / "abc" / "HEAD") is False


def test_prepare_spawn_policy_keeps_worktrees_mask_even_if_absent(tmp_path):
    # The mask + self re-expose are retained unconditionally by prepare_spawn_policy
    # (like state_mask), NOT existence-gated like git_ro_remask, so the mask cannot
    # silently drop mid-run. Contrast a vanished git_ro_remask entry, which IS
    # dropped — and git_ref_reexpose, which is re-created instead.
    gone = tmp_path / "nope" / ".git" / "worktrees"
    gone_ro = tmp_path / "gone.worktree"
    p = _policy(
        git_worktrees_mask=str(gone),
        git_worktree_self=str(gone / "self"),
        git_ro_remask=(str(gone_ro),),
    )
    pruned = prepare_spawn_policy(p)
    assert pruned.git_ro_remask == ()
    assert pruned.git_worktrees_mask == str(gone)
    assert pruned.git_worktree_self == str(gone / "self")


def test_prepare_spawn_policy_drops_a_vanished_repo_marker(tmp_path):
    # AC 22(a). A hand-pinned $IMP_STATE_DIR carries no marker by design, and bwrap
    # aborts SETUP on a missing --ro-bind source — every sandboxed step for those users
    # would die before its command ran.
    state = tmp_path / "state"
    state.mkdir()
    marker = state / "repo"
    p = _policy(state_mask=str(state), state_repo_ro=str(marker))
    prepared = prepare_spawn_policy(p)
    assert prepared.state_repo_ro == ""
    assert str(marker) not in _argv(
        "x", cwd="/wt", policy=prepared, engine_env={})


def test_prepare_spawn_policy_keeps_a_present_repo_marker(tmp_path):
    # AC 22(b): gating must not cost the marker where it exists — that is the whole
    # point of the re-expose.
    state = tmp_path / "state"
    state.mkdir()
    marker = state / "repo"
    marker.write_text(f"{tmp_path}\n")
    p = _policy(state_mask=str(state), state_repo_ro=str(marker))
    prepared = prepare_spawn_policy(p)
    assert prepared.state_repo_ro == str(marker)
    assert (str(marker), "ro") in prepared.bind_plan()
    assert f"--ro-bind {marker} {marker}" in " ".join(
        _argv("x", cwd="/wt", policy=prepared, engine_env={}))


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
    keys = {k for k, _ in pol.child_env}
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


def test_overlay_mode_true_when_userxattr_mount_succeeds(tmp_path, monkeypatch):
    # AC 3a/4a: the userxattr mount succeeds → True, after exactly ONE probe.
    import imp.engine.sandbox as sb
    monkeypatch.setattr(sb.platform, "system", lambda: "Linux")
    monkeypatch.setattr(sb.shutil, "which", lambda _: "/usr/bin/unshare")
    calls: list = []

    def fake_run(argv, **kw):
        calls.append(argv)
        return _types.SimpleNamespace(returncode=0)

    monkeypatch.setattr(sb.subprocess, "run", fake_run)
    sb.overlay_mode.cache_clear()
    assert sb.overlay_mode(tmp_path, tmp_path / "home") is True
    assert len(calls) == 1
    assert calls[0][0] == "unshare"
    assert "mount -t overlay" in calls[0][-1]
    assert ",userxattr " in calls[0][-1]
    assert not (tmp_path / ".overlay-probe").exists()


def test_overlay_mode_false_when_only_plain_mount_succeeds(tmp_path, monkeypatch):
    # AC 3b/4a: a pre-v5.11 kernel rejects userxattr; the plain re-probe succeeds
    # → False, NOT None, so the host keeps its COW home instead of dropping to a
    # writable bind of the real home.
    import imp.engine.sandbox as sb
    monkeypatch.setattr(sb.platform, "system", lambda: "Linux")
    monkeypatch.setattr(sb.shutil, "which", lambda _: "/usr/bin/unshare")
    calls: list = []

    def fake_run(argv, **kw):
        calls.append(argv)
        return _types.SimpleNamespace(returncode=0 if len(calls) > 1 else 32)

    monkeypatch.setattr(sb.subprocess, "run", fake_run)
    sb.overlay_mode.cache_clear()
    assert sb.overlay_mode(tmp_path, tmp_path / "home") is False
    assert len(calls) == 2
    assert ",userxattr " in calls[0][-1]
    assert "userxattr" not in calls[1][-1]
    assert not (tmp_path / ".overlay-probe").exists()


def test_overlay_mode_scrubs_scratch_between_attempts(tmp_path, monkeypatch):
    # S1: the second attempt starts from a clean upper/work — a file left behind
    # by the failed userxattr attempt is gone before the plain re-probe runs.
    import imp.engine.sandbox as sb
    monkeypatch.setattr(sb.platform, "system", lambda: "Linux")
    monkeypatch.setattr(sb.shutil, "which", lambda _: "/usr/bin/unshare")
    upper = tmp_path / ".overlay-probe" / "upper"
    seen: dict = {}
    calls: list = []

    def fake_run(argv, **kw):
        calls.append(argv)
        if len(calls) == 1:
            (upper / "stale").write_text("x")
            return _types.SimpleNamespace(returncode=32)
        seen["stale"] = (upper / "stale").exists()
        seen["upper_is_dir"] = upper.is_dir()
        return _types.SimpleNamespace(returncode=0)

    monkeypatch.setattr(sb.subprocess, "run", fake_run)
    sb.overlay_mode.cache_clear()
    assert sb.overlay_mode(tmp_path, tmp_path / "home") is False
    assert seen == {"stale": False, "upper_is_dir": True}


def test_overlay_mode_none_when_both_mounts_fail(tmp_path, monkeypatch):
    # AC 3c: neither shape mounts → no unprivileged overlay here.
    import imp.engine.sandbox as sb
    monkeypatch.setattr(sb.platform, "system", lambda: "Linux")
    monkeypatch.setattr(sb.shutil, "which", lambda _: "/usr/bin/unshare")
    calls: list = []

    def fake_run(argv, **kw):
        calls.append(argv)
        return _types.SimpleNamespace(returncode=32)

    monkeypatch.setattr(sb.subprocess, "run", fake_run)
    sb.overlay_mode.cache_clear()
    assert sb.overlay_mode(tmp_path, tmp_path / "home") is None
    assert len(calls) == 2
    assert not (tmp_path / ".overlay-probe").exists()


def test_overlay_mode_none_without_unshare(tmp_path, monkeypatch):
    # AC 3e
    import imp.engine.sandbox as sb
    monkeypatch.setattr(sb.platform, "system", lambda: "Linux")
    monkeypatch.setattr(sb.shutil, "which", lambda _: None)
    sb.overlay_mode.cache_clear()
    assert sb.overlay_mode(tmp_path, tmp_path / "home") is None


def test_overlay_mode_none_on_non_linux(tmp_path, monkeypatch):
    # AC 3d
    import imp.engine.sandbox as sb
    monkeypatch.setattr(sb.platform, "system", lambda: "Darwin")
    sb.overlay_mode.cache_clear()
    assert sb.overlay_mode(tmp_path, tmp_path / "home") is None


def test_overlay_mode_none_when_subprocess_raises(tmp_path, monkeypatch):
    # AC 3f/4b: the host cannot run the probe at all — short-circuit to None
    # WITHOUT a second attempt (the retry exists for a rejecting kernel, not a
    # broken spawn), and leave no scratch dir behind.
    import imp.engine.sandbox as sb
    monkeypatch.setattr(sb.platform, "system", lambda: "Linux")
    monkeypatch.setattr(sb.shutil, "which", lambda _: "/usr/bin/unshare")
    calls: list = []

    def boom(*a, **k):
        calls.append(a)
        raise OSError("no userns")

    monkeypatch.setattr(sb.subprocess, "run", boom)
    sb.overlay_mode.cache_clear()
    assert sb.overlay_mode(tmp_path, tmp_path / "home") is None
    assert len(calls) == 1
    assert not (tmp_path / ".overlay-probe").exists()


def test_overlay_mode_none_when_probe_times_out(tmp_path, monkeypatch):
    # AC 3f/4b: a TimeoutExpired is the same short-circuit — worst-case probe
    # latency stays 2 x 10s only on a genuinely userxattr-rejecting host.
    import imp.engine.sandbox as sb
    monkeypatch.setattr(sb.platform, "system", lambda: "Linux")
    monkeypatch.setattr(sb.shutil, "which", lambda _: "/usr/bin/unshare")
    calls: list = []

    def slow(*a, **k):
        calls.append(a)
        raise sb.subprocess.TimeoutExpired(cmd=["unshare"], timeout=10)

    monkeypatch.setattr(sb.subprocess, "run", slow)
    sb.overlay_mode.cache_clear()
    assert sb.overlay_mode(tmp_path, tmp_path / "home") is None
    assert len(calls) == 1


def test_overlay_mode_probes_with_real_home_as_lowerdir(tmp_path, monkeypatch):
    import imp.engine.sandbox as sb
    monkeypatch.setattr(sb.platform, "system", lambda: "Linux")
    monkeypatch.setattr(sb.shutil, "which", lambda _: "/usr/bin/unshare")
    calls: dict = {}

    def fake_run(argv, **kw):
        calls["argv"] = argv
        return _types.SimpleNamespace(returncode=0)

    monkeypatch.setattr(sb.subprocess, "run", fake_run)
    sb.overlay_mode.cache_clear()
    home = tmp_path / "agent-home"
    assert sb.overlay_mode(tmp_path, home) is True
    mount_cmd = calls["argv"][-1]
    # probes with the REAL home as lowerdir — the same shape the keeper's mount
    # in build_sandbox_spawn uses — not a synthetic empty lower under the probe dir.
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


def test_build_child_env_dedups_overlapping_env_passthrough(tmp_path):
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
            str(Path(tempfile.gettempdir()) / f"imp-{os.getuid()}" / "abc")) in pol.child_env
    # No key appears more than once.
    keys = [k for k, _ in pol.child_env]
    assert len(keys) == len(set(keys))


def test_build_child_env_passes_pythonpath_from_engine_env(tmp_path):
    control, wt = _layout(tmp_path)
    pol = resolve_policy(
        worktree=wt, control_root=control, quest_id="abc",
        cfg=SandboxConfig(),
        engine_env_result={"IMP_PROJECT_ROOT": str(wt),
                           "PYTHONPATH": str(wt / "src")},
        environ={"PATH": "/usr/bin"},
        available=True,
    )
    assert ("PYTHONPATH", str(wt / "src")) in pol.child_env
    assert ("IMP_PROJECT_ROOT", str(wt)) in pol.child_env


def test_build_child_env_omits_pythonpath_when_engine_env_lacks_it(tmp_path):
    control, wt = _layout(tmp_path)
    pol = resolve_policy(
        worktree=wt, control_root=control, quest_id="abc",
        cfg=SandboxConfig(),
        engine_env_result={"IMP_PROJECT_ROOT": str(wt)},
        environ={"PATH": "/usr/bin"},
        available=True,
    )
    keys = {k for k, _ in pol.child_env}
    assert "PYTHONPATH" not in keys


@pytest.mark.real_sandbox_policy
def test_build_sandbox_policy_drops_ambient_imp_vars(tmp_path, monkeypatch):
    # AC 4(a, d): through the PRODUCTION bake — this is the re-pointed
    # test_build_child_env_drops_ambient_imp_vars. An ambient IMP_* var must not reach
    # the sandboxed agent; an engine-authored var still arrives authoritatively; a var
    # named in env_passthrough still passes through from the environment. It formerly
    # called resolve_policy with a hand-built engine_env_result and a synthetic
    # environ — an input production never supplied — so it passed while production
    # handed the whole ambient IMP_* set to every agent and gate.
    import imp.engine.executor as ex
    monkeypatch.setattr(ex, "sandbox_available", lambda: True)
    from imp.engine import sandbox as _sb
    _sb.sandbox_available.cache_clear()
    monkeypatch.setenv("IMP_TELEGRAM_TOKEN", "canary-42:secret")
    monkeypatch.setenv("IMP_DEBUG", "verbose")
    monkeypatch.setenv("IMP_ALLOWED", "yes")

    quest = _fake_quest(tmp_path)
    pol = ex._build_sandbox_policy(
        quest, _fake_config(sandbox={"env_passthrough": ["IMP_ALLOWED"]}),
        _CaptureLog())

    child = dict(pol.child_env)
    assert "IMP_TELEGRAM_TOKEN" not in child
    assert "IMP_DEBUG" not in child
    assert child["IMP_PROJECT_ROOT"] == str(quest.project_root)
    assert child["IMP_ALLOWED"] == "yes"


def test_child_env_pythonpath_puts_the_engine_prefix_ahead_of_the_ambient(tmp_path):
    # AC 9(a): the bake merges the engine's worktree prefix with the ambient
    # PYTHONPATH exactly as overlay_engine_env does — prefix entries first, empty
    # and prefix-duplicating inherited entries dropped, the rest in order.
    control, wt = _layout(tmp_path)
    prefix = f"{wt}/src{os.pathsep}{wt}/vendor"
    pol = resolve_policy(
        worktree=wt, control_root=control, quest_id="abc", cfg=SandboxConfig(),
        engine_env_result={"PYTHONPATH": prefix},
        environ={"PATH": "/usr/bin",
                 "PYTHONPATH": f"{wt}/vendor{os.pathsep}{os.pathsep}/amb/lib"},
        available=True,
    )
    assert dict(pol.child_env)["PYTHONPATH"] == (
        f"{wt}/src{os.pathsep}{wt}/vendor{os.pathsep}/amb/lib")


def test_sandboxed_child_gets_imp_home_from_the_engine_not_the_environment(tmp_path):
    # AC 7(a): the child's `environ` carries no IMP_HOME (an active sandbox inherits
    # nothing ambient), so the value can only come from engine_env's emission —
    # without it the in-sandbox `imp` shim would fall back to ~/.imp and read another
    # home's global config.
    control, wt = _layout(tmp_path)
    eng = engine_env(project_root=wt, quest_id="abc", quest_title="T")
    pol = resolve_policy(
        worktree=wt, control_root=control, quest_id="abc", cfg=SandboxConfig(),
        engine_env_result=eng, environ={"PATH": "/usr/bin"}, available=True)
    spawn = build_sandbox_spawn("x", cwd=str(wt), policy=pol, engine_env=eng,
                                environ={})
    assert spawn.env["IMP_HOME"] == str(imp_home())


def test_sandboxed_child_gets_a_custom_imp_home(tmp_path, monkeypatch):
    # AC 7(b): a custom $IMP_HOME (not ~/.imp) reaches the sandboxed child by engine
    # emission, so in-sandbox `imp` resolves the same home as the engine.
    monkeypatch.setenv("IMP_HOME", str(tmp_path / "alt-home"))
    control, wt = _layout(tmp_path)
    eng = engine_env(project_root=wt, quest_id="abc", quest_title="T")
    pol = resolve_policy(
        worktree=wt, control_root=control, quest_id="abc", cfg=SandboxConfig(),
        engine_env_result=eng, environ={"PATH": "/usr/bin"}, available=True)
    spawn = build_sandbox_spawn("x", cwd=str(wt), policy=pol, engine_env=eng,
                                environ={})
    assert spawn.env["IMP_HOME"] == str(tmp_path / "alt-home")


def test_repo_config_names_the_e2e_poll_timeout_passthrough(tmp_path, monkeypatch):
    # AC 10: with engine_env the sole IMP_* authority, the one ambient IMP_* var the
    # in-sandbox test run wants reaches the child only because THIS repo's config
    # names it; with no such entry it does not. Read through parse_config_layer, imp's
    # OWN project-layer parser (it routes `sandbox:` to _parse_sandbox), so a block
    # imp would reject fails here rather than passing a raw yaml.safe_load.
    monkeypatch.setenv("IMP_E2E_POLL_TIMEOUT", "180")
    layer, _src = parse_config_layer(
        Path(__file__).resolve().parents[1] / "imp" / "config.yaml")
    control, wt = _layout(tmp_path)

    def _bake(cfg):
        return dict(resolve_policy(
            worktree=wt, control_root=control, quest_id="abc", cfg=cfg,
            engine_env_result={}, environ=os.environ, available=True).child_env)

    assert _bake(sandbox_config_from_raw(layer["sandbox"]))["IMP_E2E_POLL_TIMEOUT"] == "180"
    assert "IMP_E2E_POLL_TIMEOUT" not in _bake(SandboxConfig())


def test_build_child_env_strips_provider_api_keys(tmp_path):
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
    keys = {k for k, _ in pol.child_env}
    assert "ANTHROPIC_API_KEY" not in keys
    assert "OPENAI_API_KEY" not in keys


def test_build_child_env_ambient_engine_managed_imp_var_does_not_leak(tmp_path):
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
    keys = {k for k, _ in pol.child_env}
    assert "IMP_TEST_CMD_FAST" not in keys


def test_build_child_env_engine_imp_var_wins_over_ambient(tmp_path):
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
    assert ("IMP_TEST_CMD_FAST", "real-gate") in pol.child_env
    assert ("IMP_TEST_CMD_FAST", "stale-gate") not in pol.child_env


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
    wt = project_state_dir(control) / "worktrees" / "abc"
    wt.mkdir(parents=True, exist_ok=True)
    (control / ".git").mkdir(parents=True, exist_ok=True)
    return _types.SimpleNamespace(
        project_root=wt, control_root=control, id="abc",
        handle="abc-slug", title="Abc Title",
        quest_dir=project_state_dir(control) / "quests" / "abc",
        tmp_dir=Path(tempfile.gettempdir()) / f"imp-{os.getuid()}" / "abc")


def test_fake_quest_home_roots_at_the_state_dir(tmp_path):
    quest = _fake_quest(tmp_path)
    state = project_state_dir(quest.control_root)
    assert quest.quest_dir == state / "quests" / "abc"
    assert str(state / "worktrees") not in str(quest.quest_dir)


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
    the policy's child_env as IMP_PROJECT_ROOT."""
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
    assert ("IMP_PROJECT_ROOT", str(quest.project_root)) in pol.child_env
    # active is the silent common case — no posture event is emitted.
    assert not any(e["type"] in ("sandbox_inactive", "sandbox_unavailable")
                   for e in log.events)


@pytest.mark.real_sandbox_policy
def test_build_sandbox_policy_threads_pythonpath_to_child_env(tmp_path, monkeypatch):
    """AC 9(a): config.pythonpath reaches the policy's child_env as the
    worktree-resolved prefix merged ahead of the ambient PYTHONPATH — prefix first,
    empty entries dropped, surviving ambient entries in order."""
    import imp.engine.executor as ex
    monkeypatch.setattr(ex, "sandbox_available", lambda: True)
    from imp.engine import sandbox as _sb
    _sb.sandbox_available.cache_clear()

    quest = _fake_quest(tmp_path)
    config = _fake_config(pythonpath=("src",))
    monkeypatch.setenv("PYTHONPATH", f"/amb/lib{os.pathsep}{os.pathsep}/amb/two")
    log = _CaptureLog()
    pol = ex._build_sandbox_policy(quest, config, log)

    assert dict(pol.child_env)["PYTHONPATH"] == (
        f"{quest.project_root / 'src'}{os.pathsep}/amb/lib{os.pathsep}/amb/two")


@pytest.mark.real_sandbox_policy
def test_build_sandbox_policy_bakes_no_pythonpath_when_none_configured(
        tmp_path, monkeypatch):
    # AC 9(b): PYTHONPATH reaches the sandbox because engine_env EMITS it, never by
    # inheritance — with no configured pythonpath, an ambient one is not baked.
    import imp.engine.executor as ex
    monkeypatch.setattr(ex, "sandbox_available", lambda: True)
    from imp.engine import sandbox as _sb
    _sb.sandbox_available.cache_clear()
    monkeypatch.setenv("PYTHONPATH", "/amb/lib")

    pol = ex._build_sandbox_policy(
        _fake_quest(tmp_path), _fake_config(), _CaptureLog())

    assert "PYTHONPATH" not in dict(pol.child_env)


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
    """Recover the dest-keyed (path, kind) path-covering binds from a real build_sandbox_spawn
    emission — the inverse of build_sandbox_spawn's kind→flag map. Scans the bind
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
    # Drift guard: the two consumers of the bind order — build_sandbox_spawn's argv
    # and policy.bind_plan() (which allows_write evaluates) — must agree exactly.
    # Parsing the emitted argv back to (path, kind) and comparing to bind_plan()
    # means a new bind added to the plan MUST surface in argv (and vice-versa), or
    # this fails. _mirror_policy exercises every kind: rw / ro / mask_dir / mask_file.
    p = _mirror_policy()
    argv = _argv("x", cwd="/wt", policy=p, engine_env={})
    assert _parse_argv_binds(argv) == p.bind_plan()


def test_home_layer_rw_binds_real_home_first():
    # AC 8 (unit): the fallback writable-home layer emits a rw bind of the real
    # home, ordered BEFORE the worktree / cache / secret binds so later binds win
    # (caches write through, worktree stays real, secrets still mask).
    pol = _policy(home="/home/u", home_layer=HomeLayer(home="/home/u"))
    argv = _argv("x", cwd="/wt", policy=pol, engine_env={})
    assert argv[0] == "bwrap"                      # no unshare wrapper for rw fallback
    flat = " ".join(argv)
    assert "--bind /home/u /home/u" in flat
    i_home = argv.index("/home/u")                 # first "/home/u" is the bind source
    i_wt = argv.index("/state/worktrees/abc")
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
    flat = " ".join(_argv("x", cwd="/wt", policy=pol, engine_env={}))
    assert "--bind /home/u /home/u" not in flat
    assert not pol.allows_write(Path("/home/u/newfile"))


def test_argv_binds_match_bind_plan_with_home_layer():
    # Drift guard extended to the home bind: the rw fallback's source == dest, so
    # the parse-back still equals bind_plan() (which is dest-keyed).
    pol = _policy(home="/home/u", home_layer=HomeLayer(home="/home/u"))
    argv = _argv("x", cwd="/wt", policy=pol, engine_env={})
    assert _parse_argv_binds(argv) == pol.bind_plan()


_UNSHARE_PREFIX = ["unshare", "--user", "--map-root-user", "--mount", "sh", "-c"]


def _unshare_script(argv: list[str]) -> str:
    assert argv[:len(_UNSHARE_PREFIX)] == _UNSHARE_PREFIX
    return argv[len(_UNSHARE_PREFIX)]


def test_overlay_home_wraps_bwrap_with_unshare_mount():
    # AC 1/2: a COW overlay is composed by wrapping bwrap with an unprivileged
    # overlay mount; the merged view is bound onto the REAL home path.
    hl = HomeLayer(home="/home/u", merged="/up/merged",
                   upper="/up/upper", work="/up/work")
    pol = _policy(home="/home/u", home_layer=hl)
    argv = _argv("echo hi", cwd="/wt", policy=pol, engine_env={})
    script = _unshare_script(argv)
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
    argv = _argv("x", cwd="/wt", policy=pol, engine_env={})
    script = _unshare_script(argv)
    assert "lowerdir='/ho me'" in script          # lower (real home) quoted
    assert "upperdir='/a b/upper'" in script       # upper quoted
    assert "workdir='/a b/work'" in script         # work quoted
    assert "'/a b/merged'" in script               # mount target + bind source quoted


def test_overlay_home_userxattr_appends_mount_option():
    # AC 2a: a userxattr layer moves the opaque marker to user.overlay.*; the
    # option is appended to the mount options, immediately after workdir=.
    hl = HomeLayer(home="/home/u", merged="/up/merged",
                   upper="/up/upper", work="/up/work", userxattr=True)
    pol = _policy(home="/home/u", home_layer=hl)
    argv = _argv("echo hi", cwd="/wt", policy=pol, engine_env={})
    script = _unshare_script(argv)
    assert ("mount -t overlay overlay -o "
            "lowerdir=/home/u,upperdir=/up/upper,workdir=/up/work,userxattr "
            "/up/merged") in script
    # the fail-loud contract and the merged-onto-home bind are untouched
    assert "|| exit 1" in script
    assert "--bind /up/merged /home/u" in script


def test_overlay_home_without_userxattr_emits_no_userxattr_option():
    # AC 2b: the default layer keeps today's option string byte-for-byte, and
    # `userxattr` appears NOWHERE in the argv — a pre-v5.11 kernel rejects the
    # option outright, so it must not leak in.
    hl = HomeLayer(home="/home/u", merged="/up/merged",
                   upper="/up/upper", work="/up/work")
    pol = _policy(home="/home/u", home_layer=hl)
    argv = _argv("echo hi", cwd="/wt", policy=pol, engine_env={})
    assert ("mount -t overlay overlay -o "
            "lowerdir=/home/u,upperdir=/up/upper,workdir=/up/work "
            "/up/merged") in _unshare_script(argv)
    assert not any("userxattr" in a for a in argv)


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
    argv = _argv("x", cwd="/wt", policy=pol, engine_env={})
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


def test_sandbox_failure_detail_is_the_first_nonempty_line_clipped():
    # The payload field is one line of diagnosis: bwrap's own reason, never the
    # blank padding around it, never more than 200 chars of a pathological line.
    from imp.engine.sandbox import sandbox_failure_detail
    stderr = ("\n   \n"
              "bwrap: Can't bind mount /x: No such file or directory\n"
              "and a second line that must not be carried\n")
    assert sandbox_failure_detail(stderr) == (
        "bwrap: Can't bind mount /x: No such file or directory")
    assert sandbox_failure_detail("x" * 250 + "\nnext") == "x" * 200
    assert sandbox_failure_detail("  \n\t\n") == ""      # nothing said → nothing shown
    assert sandbox_failure_detail("") == ""


def test_spawn_failed_message_no_longer_claims_transience():
    # AC 9a: the string only ever prints AFTER five retries proved otherwise.
    from imp.engine.sandbox import SANDBOX_SPAWN_FAILED_MESSAGE
    assert SANDBOX_SPAWN_FAILED_MESSAGE == (
        "the OS sandbox could not start your command; it did NOT run "
        "(a sandbox-setup failure, not your command's own exit)")


def test_sandbox_spawn_backoff_bounds_and_cap():
    from imp.engine.sandbox import (
        SANDBOX_SPAWN_MAX_ATTEMPTS,
        sandbox_spawn_backoff_delay,
    )
    assert SANDBOX_SPAWN_MAX_ATTEMPTS == 5
    # Full-jitter exponential ceiling per retry: 50ms → 100 → 200 → 400ms.
    ceilings = [0.05, 0.1, 0.2, 0.4]
    for attempt, ceiling in enumerate(ceilings, start=1):
        samples = [sandbox_spawn_backoff_delay(attempt) for _ in range(64)]
        assert all(0.0 <= s <= ceiling for s in samples)      # never exceeds the ceiling
        assert len({round(s, 6) for s in samples}) > 1        # jitter, not a constant
    # The ceiling caps at 400ms — no further doubling is exposed past the 4th retry.
    assert all(sandbox_spawn_backoff_delay(9) <= 0.4 for _ in range(32))
    # Worst-case total added latency before a pause stays under ~1s (4 retries).
    assert sum(ceilings) < 1.0


def test_overlay_wrapper_omits_sentinel_on_mount_failure():
    from imp.engine.sandbox import SANDBOX_ENTERED_MARKER
    hl = HomeLayer(home="/home/u", merged="/up/merged",
                   upper="/up/upper", work="/up/work")
    pol = _policy(home="/home/u", home_layer=hl)
    argv = _argv("echo hi", cwd="/wt", policy=pol, engine_env={})
    script = _unshare_script(argv)
    assert " && exec" not in script
    assert "|| exit" in script
    assert "exec bwrap" in script
    assert ">&2" in script
    assert SANDBOX_ENTERED_MARKER in script
    assert script.index("exec bwrap") < script.index(SANDBOX_ENTERED_MARKER)


def test_overlay_wrapper_passes_the_command_through_shlex_join():
    # AC 7b: the unshare/mount form re-quotes the whole argv with shlex.join, so
    # the command argument must survive it byte-identically — parsing the exec'd
    # tail back out must yield the same four elements the plain form emits.
    import shlex

    from imp.engine.sandbox import SANDBOX_ENTERED_MARKER
    hl = HomeLayer(home="/home/u", merged="/up/merged",
                   upper="/up/upper", work="/up/work")
    pol = _policy(home="/home/u", home_layer=hl)
    cmd = 'echo "hi there"; false'
    argv = _argv(cmd, cwd="/wt", policy=pol, engine_env={})
    script = _unshare_script(argv)
    inner = shlex.split(script.split("exec ", 1)[1])
    assert inner[0] == "bwrap"
    assert inner[-4:-2] == ["sh", "-c"]
    assert inner[-2] == (
        f"printf '%s\\n' {shlex.quote(SANDBOX_ENTERED_MARKER)} >&2; "
        'exec sh -c "$0"')
    assert inner[-1] == cmd


def test_make_home_layer_overlay_creates_the_root_and_cleanup_sweeps_it(tmp_path):
    # AC 10 (unit): the primitive creates only the ROOT and reports it — with the
    # probed userxattr shape — as overlay_root; the layer itself is plain (each
    # spawn mints its own under the root). The one cleanup rmtrees the whole root —
    # including the mode-000 work/work dir overlayfs leaves behind (AC 6).
    root = tmp_path / "coder.overlay"
    layer, overlay_root, cleanup = make_home_layer(
        "/home/u", root=root, userxattr=True)
    assert layer == HomeLayer(home="/home/u") and not layer.is_overlay
    assert overlay_root == OverlayRoot(path=str(root), userxattr=True)
    assert root.is_dir()
    residue = root / "spawn-a" / "work" / "work"
    residue.mkdir(parents=True)
    residue.chmod(0o000)
    cleanup()
    assert not root.exists()


def test_make_home_layer_overlay_without_userxattr(tmp_path):
    # AC 5b: userxattr=False is still a COW overlay (a pre-v5.11 host keeps home
    # isolation) — same root, same cleanup, flag off.
    root = tmp_path / "coder.overlay"
    layer, overlay_root, cleanup = make_home_layer(
        "/home/u", root=root, userxattr=False)
    assert layer == HomeLayer(home="/home/u") and not layer.is_overlay
    assert overlay_root == OverlayRoot(path=str(root), userxattr=False)
    assert root.is_dir()
    cleanup()
    assert not root.exists()


def test_make_home_layer_fallback_creates_no_root(tmp_path):
    # AC 5a/8/10 (unit): userxattr=None is the rw fallback — no disk touched, no
    # overlay root, and a harmless no-op cleanup.
    root = tmp_path / "coder.overlay"
    layer, overlay_root, cleanup = make_home_layer(
        "/home/u", root=root, userxattr=None)
    assert layer == HomeLayer(home="/home/u") and not layer.is_overlay
    assert overlay_root is None
    assert not root.exists()
    cleanup()  # no-op, must not raise
    assert not root.exists()


def test_force_rmtree_skips_symlinked_dirs(tmp_path: Path) -> None:
    # Symlinks to dirs appear in os.walk dirs; os.chmod follows them.
    # Verify the guard prevents mutating a directory outside the removed tree.
    outside = tmp_path / "outside"
    outside.mkdir()
    original_mode = os.stat(outside).st_mode
    tree = tmp_path / "tree"
    tree.mkdir()
    (tree / "real_subdir").mkdir()
    (tree / "link_to_outside").symlink_to(outside)
    _force_rmtree(tree)
    assert not tree.exists()
    assert outside.exists()
    assert os.stat(outside).st_mode == original_mode


def test_prepare_spawn_policy_mints_a_distinct_layer_per_spawn(tmp_path):
    # AC 1a/1b: two spawns off ONE step policy get three-way-distinct
    # upper/work/merged dirs, all real and all under that step's overlay_root —
    # so two concurrently-live mounts can never name the same upperdir. Each
    # minted layer carries the root's probed userxattr shape (AC 6).
    root = tmp_path / "coder.overlay"
    step_pol = _policy(home="/home/u", home_layer=HomeLayer(home="/home/u"),
                       overlay_root=OverlayRoot(path=str(root), userxattr=True))
    a = prepare_spawn_policy(step_pol)
    b = prepare_spawn_policy(step_pol)
    a_dirs = (a.home_layer.upper, a.home_layer.work, a.home_layer.merged)
    b_dirs = (b.home_layer.upper, b.home_layer.work, b.home_layer.merged)
    for d in (*a_dirs, *b_dirs):
        assert Path(d).is_dir()
        assert Path(d).is_relative_to(root)
    assert len(set(a_dirs) | set(b_dirs)) == 6      # no path reused anywhere
    assert a.home_layer.is_overlay and b.home_layer.is_overlay
    assert a.home_layer.home == "/home/u" == b.home_layer.home
    assert a.home_layer.userxattr is True and b.home_layer.userxattr is True
    assert a.overlay_root == b.overlay_root == step_pol.overlay_root


def test_each_minted_layer_reaches_its_own_mount_command(tmp_path):
    # AC 1c: the per-spawn dirs are what actually get mounted — each policy's own
    # upper/work/merged appear verbatim in ITS overlay mount command, and neither
    # spawn's argv ever names the other spawn's upperdir.
    import shlex

    root = tmp_path / "coder.overlay"
    step_pol = _policy(home="/home/u", home_layer=HomeLayer(home="/home/u"),
                       overlay_root=OverlayRoot(path=str(root), userxattr=False))
    a, b = prepare_spawn_policy(step_pol), prepare_spawn_policy(step_pol)
    scripts = {
        p: _unshare_script(
            _argv("x", cwd="/wt", policy=p, engine_env={}))
        for p in (a, b)}
    for pol, other in ((a, b), (b, a)):
        script, hl = scripts[pol], pol.home_layer
        assert "lowerdir=/home/u," in script
        assert (f"upperdir={shlex.quote(hl.upper)},"
                f"workdir={shlex.quote(hl.work)} "
                f"{shlex.quote(hl.merged)}") in script
        assert other.home_layer.upper not in script


def test_prepare_spawn_policy_self_heals_a_removed_overlay_root(tmp_path):
    # AC 5: the root vanishing mid-step must not fail every later spawn
    # permanently (the retry loop cannot heal a permanent setup failure), so the
    # mint re-creates it — same shape as the git_ref_reexpose re-creation above it.
    root = tmp_path / "gone" / "coder.overlay"
    step_pol = _policy(home="/home/u", home_layer=HomeLayer(home="/home/u"),
                       overlay_root=OverlayRoot(path=str(root), userxattr=False))
    assert not root.exists()
    spawn = prepare_spawn_policy(step_pol)
    upper = Path(spawn.home_layer.upper)
    assert upper.is_dir() and upper.is_relative_to(root)


def test_prepare_spawn_policy_mints_nothing_without_an_overlay_root():
    # AC 8: the non-overlay host's shape — a plain rw-bind home layer and no
    # overlay_root — passes through untouched, so no overlay is mounted.
    pol = _policy(home="/home/u", home_layer=HomeLayer(home="/home/u"))
    out = prepare_spawn_policy(pol)
    assert out.home_layer == HomeLayer(home="/home/u")
    assert out.overlay_root is None
    argv = _argv("x", cwd="/wt", policy=out, engine_env={})
    assert argv[0] == "bwrap"          # no unshare/mount wrapper


def test_active_overlay_root_without_a_minted_layer_is_fatal(tmp_path):
    # AC 2a: the fail-OPEN shape — an ACTIVE policy carrying overlay_root but a
    # layer that was never minted — would emit `--bind /home/u /home/u`, binding
    # the REAL $HOME writable with no overlay at all. Refuse to build that argv.
    pol = _policy(home="/home/u", home_layer=HomeLayer(home="/home/u"),
                  overlay_root=OverlayRoot(path=str(tmp_path / "coder.overlay"),
                                           userxattr=True))
    with pytest.raises(AssertionError, match="overlay_root"):
        _argv("echo hi", cwd="/wt", policy=pol, engine_env={})


def test_resolve_policy_binds_quest_home_rw(tmp_path):
    control, wt = _layout(tmp_path)
    home = project_state_dir(control) / "quests" / "abc"
    pol = resolve_policy(
        worktree=wt, control_root=control, quest_id="abc",
        cfg=SandboxConfig(), engine_env_result={}, environ={},
        available=True, quest_dir=home,
    )
    assert pol.quest_home == str(home)
    # An artifact write under the home is permitted by the in-process mirror...
    assert pol.allows_write(home / "artifacts" / "spec" / "x.md")
    # ...and the bwrap argv carries the matching rw bind.
    argv = " ".join(_argv("x", cwd=str(wt), policy=pol, engine_env={}))
    assert f"--bind {home} {home}" in argv


def test_resolve_policy_masks_own_steps_not_siblings(tmp_path):
    control, wt = _layout(tmp_path)
    quest_dir = project_state_dir(control) / "quests" / "abc"
    (quest_dir / "steps").mkdir(parents=True)
    (quest_dir / "artifacts").mkdir()
    (quest_dir / "events.jsonl").write_text("")
    sib_dir = project_state_dir(control) / "quests" / "sib"
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
    # The sibling is nonetheless denied in-mirror by the whole-state tmpfs.
    assert pol.allows_read(sib_dir / "steps" / "x") is False
    assert pol.allows_read(sib_dir / "events.jsonl") is False


def test_resolve_policy_allows_read_reflects_steps_mask(tmp_path):
    control, wt = _layout(tmp_path)
    quest_dir = project_state_dir(control) / "quests" / "abc"
    (quest_dir / "steps").mkdir(parents=True)
    (quest_dir / "artifacts").mkdir()
    (quest_dir / "workflow").mkdir()
    (quest_dir / "events.jsonl").write_text("")
    sib_dir = project_state_dir(control) / "quests" / "sib"
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
    quest_dir = project_state_dir(control) / "quests" / "abc"
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
    quest_dir = project_state_dir(control) / "quests" / "abc"
    quest_dir.mkdir(parents=True)
    (quest_dir / "events.jsonl").write_text('{"e":1}\n')
    sib_dir = project_state_dir(control) / "quests" / "sib"
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
    quest_dir = project_state_dir(control) / "quests" / "abc"
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
        worktree="/state/worktrees/abc",
        quest_home="/state/quests/abc",
        git_common_dir="/ctl/.git",
        git_ro_remask=(), writable_caches=(), extra_readable=(),
        extra_writable=(), secret_dirs=(), secret_files=(),
        readonly_overlays=("/state/worktrees/abc",),
    )
    # Whole-worktree ro overlay: source is locked...
    assert p.allows_write(Path("/state/worktrees/abc/src/x.py")) is False
    # ...but quest_home lives OUTSIDE the worktree, so it stays rw.
    assert p.allows_write(Path("/state/quests/abc/progress.md")) is True


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
    argv = _argv("x", cwd="/wt", policy=p, engine_env={})
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
    argv = _argv("x", cwd="/wt", policy=p, engine_env={})
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
    argv = _argv("x", cwd="/wt", policy=p, engine_env={})
    assert _parse_argv_binds(argv) == p.bind_plan()
    assert ("/ctl/.git/refs/heads/imp", "rw") in p.bind_plan()


def test_headless_vars_flow_through_build_sandbox_spawn():
    # The four headless vars ride the live per-spawn engine_env; the allowlist
    # (_is_engine_passthrough) admits them into the child environment.
    spawn = build_sandbox_spawn(
        "x", cwd="/wt", policy=_policy(),
        engine_env={"GIT_EDITOR": "true", "GIT_TERMINAL_PROMPT": "0",
                    "EDITOR": "true", "VISUAL": "true"}, environ={})
    assert spawn.env["GIT_EDITOR"] == "true"
    assert spawn.env["GIT_TERMINAL_PROMPT"] == "0"
    assert spawn.env["EDITOR"] == "true"
    assert spawn.env["VISUAL"] == "true"


def test_build_child_env_bakes_headless_vars_from_engine_env(tmp_path):
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
    assert ("GIT_EDITOR", "true") in pol.child_env
    assert ("GIT_TERMINAL_PROMPT", "0") in pol.child_env
    assert ("EDITOR", "true") in pol.child_env
    assert ("VISUAL", "true") in pol.child_env


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
    # bind_plan/build_sandbox_spawn refactor that drops it is caught.
    control, _wt = _layout(tmp_path)
    git = control / ".git"
    (git / "refs" / "heads").mkdir(parents=True)
    (git / "index").write_text("")
    (git / "refs" / "stash").write_text("")
    pol = resolve_policy(worktree=control, control_root=control, quest_id="abc",
                         cfg=SandboxConfig(), engine_env_result={}, environ={},
                         available=True)
    argv = _argv(
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
    quest_dir = project_state_dir(control) / "quests" / "abc"
    (quest_dir / "workflow").mkdir(parents=True)
    pol = resolve_policy(
        worktree=wt, control_root=control, quest_id="abc",
        cfg=SandboxConfig(), engine_env_result={}, environ={},
        available=True, quest_dir=quest_dir,
    )
    assert str(quest_dir / "workflow") in pol.readonly_overlays


def test_resolve_policy_omits_workflow_overlay_when_snapshot_absent(tmp_path):
    control, wt = _layout(tmp_path)
    quest_dir = project_state_dir(control) / "quests" / "abc"
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
    quest_dir = project_state_dir(control) / "quests" / "abc"
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
    quest_dir = project_state_dir(control) / "quests" / "abc"
    snapshot = quest_dir / "workflow"
    snapshot.mkdir(parents=True)
    pol = resolve_policy(
        worktree=wt, control_root=control, quest_id="abc",
        cfg=SandboxConfig(), engine_env_result={}, environ={},
        available=True, quest_dir=quest_dir,
    )
    argv = " ".join(_argv("x", cwd=str(wt), policy=pol, engine_env={}))
    quest_home_bind = f"--bind {quest_dir} {quest_dir}"
    snapshot_bind = f"--ro-bind {snapshot} {snapshot}"
    assert quest_home_bind in argv
    assert snapshot_bind in argv
    assert argv.index(quest_home_bind) < argv.index(snapshot_bind)


def test_resolve_policy_omits_workflow_overlay_when_workflow_is_plain_file(tmp_path):
    control, wt = _layout(tmp_path)
    quest_dir = project_state_dir(control) / "quests" / "abc"
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
    archive = project_state_dir(control) / "archive"
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
    # ...but the whole-state tmpfs denies every archive path in the read mirror,
    # including records/ (a capability nothing used).
    assert pol.allows_read(archive / "blobs" / "ab" / "cd") is False
    assert pol.allows_read(archive / "index.db") is False
    assert pol.allows_read(archive / "records" / "x.json") is False


def test_resolve_policy_sibling_appearance_and_removal_are_noop(tmp_path):
    # AC 7: a sibling quest appearing or vanishing after resolve_policy changes
    # NOTHING about the current quest's resolved policy or emitted argv — the mask
    # race is gone by construction (no sibling enumeration).
    control, wt = _layout(tmp_path)
    quest_dir = project_state_dir(control) / "quests" / "abc"
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
    argv1 = _argv("x", cwd=str(wt), policy=pol1, engine_env={})

    # Sibling APPEARS with its own steps/ + events.jsonl.
    sib = project_state_dir(control) / "quests" / "other"
    (sib / "steps").mkdir(parents=True)
    (sib / "events.jsonl").write_text('{"e":1}\n')
    pol2 = _resolve()
    argv2 = _argv("x", cwd=str(wt), policy=pol2, engine_env={})
    assert pol2 == pol1
    assert argv2 == argv1

    # Sibling VANISHES.
    import shutil as _shutil
    _shutil.rmtree(sib)
    pol3 = _resolve()
    assert pol3 == pol1


def test_bind_plan_imp_tmpfs_before_worktree_for_nested_worktree():
    # Normal quest: worktree nested at <state>/worktrees/abc, quest home at
    # <state>/quests/abc. The tmpfs must precede the worktree rw bind, and both the
    # worktree and quest_home rebinds must follow it (last-wins re-exposes them).
    p = _policy(state_mask="/state", quest_home="/state/quests/abc")
    paths = [b for b, _ in p.bind_plan()]
    i_tmpfs = paths.index("/state")
    i_wt = paths.index("/state/worktrees/abc")
    i_home = paths.index("/state/quests/abc")
    assert i_tmpfs < i_wt < i_home
    assert p.bind_plan()[i_tmpfs][1] == "mask_dir"


def test_bind_plan_imp_tmpfs_after_worktree_for_draft():
    # Draft/degenerate agent: worktree == control_root, outside the state mask. The
    # control-root rw bind must precede the tmpfs (so the tmpfs masks the state
    # dir), the quest_home rebind must follow it, and the worktree is NOT
    # re-exposed after it.
    p = _policy(state_mask="/state", worktree="/ctl",
                quest_home="/state/quests/abc")
    paths = [b for b, _ in p.bind_plan()]
    i_wt = paths.index("/ctl")
    i_tmpfs = paths.index("/state")
    i_home = paths.index("/state/quests/abc")
    assert i_wt < i_tmpfs < i_home
    assert paths.count("/ctl") == 1   # worktree bound once, before the tmpfs only


def test_argv_binds_match_bind_plan_with_imp_tmpfs():
    # Drift guard: the new tmpfs round-trips through the argv parser for BOTH the
    # nested and draft orderings.
    p = _policy(state_mask="/state", quest_home="/state/quests/abc")
    argv = _argv("x", cwd="/wt", policy=p, engine_env={})
    assert _parse_argv_binds(argv) == p.bind_plan()
    assert ("/state", "mask_dir") in p.bind_plan()

    pd = _policy(state_mask="/state", worktree="/ctl",
                 quest_home="/state/quests/abc")
    argvd = _argv("x", cwd="/ctl", policy=pd, engine_env={})
    assert _parse_argv_binds(argvd) == pd.bind_plan()


def test_allows_read_draft_tmpfs_masks_sibling_state():
    # Draft case: the worktree IS the control root and the state mask sits outside
    # it, so the tmpfs — emitted after the control-root rw bind — is what denies
    # siblings, the archive and the local config, while the re-exposed own home
    # reads True and its own steps/events masks still deny.
    p = _policy(
        state_mask="/state", worktree="/ctl",
        quest_home="/state/quests/abc",
        secret_dirs=("/state/quests/abc/steps", "/run/user/1000"),
        secret_files=("/state/quests/abc/events.jsonl",),
    )
    assert p.allows_read(Path("/state/quests/other/events.jsonl")) is False
    assert p.allows_read(Path("/state/archive/blobs/ab/cd")) is False
    assert p.allows_read(Path("/state/config.yaml")) is False
    assert p.allows_read(Path("/state/quests/abc/artifacts/spec/abc.md")) is True
    assert p.allows_read(Path("/state/quests/abc/steps/x")) is False
    assert p.allows_read(Path("/state/quests/abc/events.jsonl")) is False
    assert p.allows_write(Path("/state/quests/other/x")) is False
    assert p.allows_write(Path("/state/quests/abc/artifacts/x.md")) is True


def test_allows_read_nested_tmpfs_masks_siblings_and_archive():
    # Nested (normal) worktree: the state tmpfs precedes the worktree rw bind. A
    # sibling home and the archive read as denied; the re-exposed own home and the
    # nested worktree read True, while the own steps/events masks still deny.
    p = _policy(
        state_mask="/state", worktree="/state/worktrees/abc",
        quest_home="/state/quests/abc",
        secret_dirs=("/state/quests/abc/steps", "/run/user/1000"),
        secret_files=("/state/quests/abc/events.jsonl",),
    )
    assert p.allows_read(Path("/state/quests/other/events.jsonl")) is False
    assert p.allows_read(Path("/state/archive/blobs/ab/cd")) is False
    assert p.allows_read(Path("/state/config.yaml")) is False
    assert p.allows_read(Path("/state/quests/abc/artifacts/spec/abc.md")) is True
    assert p.allows_read(Path("/state/worktrees/abc/src/x.py")) is True
    assert p.allows_read(Path("/state/quests/abc/steps/x")) is False
    assert p.allows_read(Path("/state/quests/abc/events.jsonl")) is False


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
            str(session_socket_path("abc"))) in pol.child_env


def test_session_socket_marker_absent_when_inactive(tmp_path):
    control, wt = _layout(tmp_path)
    pol = resolve_policy(
        worktree=wt, control_root=control, quest_id="abc",
        cfg=SandboxConfig(enabled=False), engine_env_result={},
        environ={}, available=True,
    )
    assert "IMP_SESSION_SOCKET" not in {k for k, _ in pol.child_env}

    pol = resolve_policy(
        worktree=wt, control_root=control, quest_id="abc",
        cfg=SandboxConfig(allow_unsandboxed=True), engine_env_result={},
        environ={}, available=False,
    )
    assert "IMP_SESSION_SOCKET" not in {k for k, _ in pol.child_env}


def test_session_socket_marker_reaches_the_child_env(tmp_path):
    control, wt = _layout(tmp_path)
    pol = resolve_policy(
        worktree=wt, control_root=control, quest_id="abc",
        cfg=SandboxConfig(), engine_env_result={}, environ={"PATH": "/usr/bin"},
        available=True,
    )
    spawn = build_sandbox_spawn("x", cwd=str(wt), policy=pol, engine_env={},
                                environ={})
    from imp.remote.session.discover import session_socket_path
    assert spawn.env["IMP_SESSION_SOCKET"] == str(session_socket_path("abc"))


def test_build_child_env_prepends_shim_dir_to_path():
    import os

    from imp.engine.sandbox import _build_child_env
    child_env = dict(_build_child_env({}, {"PATH": "/usr/bin"}, (), shim_dir="/q/imp-shim"))
    assert child_env["PATH"] == "/q/imp-shim" + os.pathsep + "/usr/bin"


def test_build_child_env_no_shim_dir_leaves_path_untouched():
    from imp.engine.sandbox import _build_child_env
    child_env = dict(_build_child_env({}, {"PATH": "/usr/bin"}, ()))
    assert child_env["PATH"] == "/usr/bin"


def test_build_child_env_prepends_shim_dir_no_existing_path():
    from imp.engine.sandbox import _build_child_env
    child_env = dict(_build_child_env({}, {}, (), shim_dir="/q/imp-shim"))
    assert child_env["PATH"] == "/q/imp-shim"


def test_resolve_policy_prepends_shim_dir_when_quest_dir_given(tmp_path):
    import os
    control, wt = _layout(tmp_path)
    qd = project_state_dir(control) / "quests" / "abc"
    pol = resolve_policy(
        worktree=wt, control_root=control, quest_id="abc", cfg=SandboxConfig(),
        engine_env_result={}, environ={"PATH": "/usr/bin"}, available=True,
        quest_dir=qd,
    )
    path = dict(pol.child_env)["PATH"]
    assert path.startswith(str(qd / "imp-shim") + os.pathsep)
    assert path.endswith("/usr/bin")


def test_worktree_swap_flips_exposure_to_new_worktree(tmp_path):
    # Go-live refreshes the policy with a single dataclasses.replace of `worktree`.
    # Swapping the draft's control-root worktree for the nested <state>/worktrees/<id>
    # path must flip exposure from draft-shaped to live-shaped.
    control, wt = _layout(tmp_path)                 # wt = <state>/worktrees/abc
    quest_dir = project_state_dir(control) / "quests" / "abc"
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
    # (c) a sibling elsewhere under the state dir stays masked.
    sibling = project_state_dir(control) / "worktrees" / "other" / "f.py"
    assert live.allows_read(sibling) is False


def test_build_child_env_session_socket_passes_through():
    from imp.engine.sandbox import _build_child_env
    child_env = dict(_build_child_env(
        {}, {"PATH": "/usr/bin"}, (),
        session_socket="/tmp/imp-1000/abc.sock"))
    assert child_env["IMP_SESSION_SOCKET"] == "/tmp/imp-1000/abc.sock"


def test_imp_output_env_passes_through_unrewritten():
    spawn = build_sandbox_spawn(
        "x", cwd="/wt", policy=_policy(),
        engine_env={"IMP_OUTPUT": "/t/imp-1000/abc/imp-output-XY.json",
                    "IMP_PROJECT_ROOT": "/state/worktrees/abc"}, environ={})
    assert spawn.env["IMP_OUTPUT"] == "/t/imp-1000/abc/imp-output-XY.json"
    assert spawn.env["IMP_PROJECT_ROOT"] == "/state/worktrees/abc"


def test_var_tmp_bind_source_and_position():
    # Set case: the per-quest source is bound onto /var/tmp, positioned in
    # bind_plan immediately AFTER the /tmp scratch entry and before the worktree.
    pol = _policy(quest_var_tmp_dir="/vt/imp-1000/abc")
    flat = " ".join(_argv("x", cwd="/wt", policy=pol, engine_env={}))
    assert "--bind /vt/imp-1000/abc /var/tmp" in flat   # per-quest source → /var/tmp
    argv = _argv("x", cwd="/wt", policy=pol, engine_env={})
    i_tmp = argv.index("/tmp")                    # first "/tmp" is the /tmp bind source
    i_vt = argv.index("/vt/imp-1000/abc")         # the /var/tmp bind source
    i_wt = argv.index("/state/worktrees/abc")  # worktree src (default _policy worktree)
    assert i_tmp < i_vt < i_wt
    bp = pol.bind_plan()
    assert bp[bp.index(("/tmp", "rw")) + 1] == ("/var/tmp", "rw")  # right after /tmp
    assert pol.allows_write(Path("/var/tmp/scratch")) is True


def test_no_var_tmp_bind_when_unset():
    # Negative: empty quest_var_tmp_dir emits NO /var/tmp bind — host /var/tmp
    # stays on the read-only floor (no identity fallback). Drift guard still holds.
    pol = _policy()   # quest_var_tmp_dir defaults to ""
    argv = _argv("x", cwd="/wt", policy=pol, engine_env={})
    assert "/var/tmp" not in argv                       # no bind dest emitted at all
    assert ("/var/tmp", "rw") not in pol.bind_plan()
    assert pol.allows_write(Path("/var/tmp/x")) is False # uncovered → read-only floor
    assert pol.allows_read(Path("/var/tmp/x")) is True   # readable via the ro floor
    assert _parse_argv_binds(argv) == pol.bind_plan()    # argv/plan agree (no drift)


def test_argv_binds_match_bind_plan_with_var_tmp_source():
    # /tmp is now an identity bind; /var/tmp still binds a per-quest host SOURCE
    # onto its namespace DEST. Parse-back is dest-keyed, so it equals bind_plan().
    pol = _policy(quest_var_tmp_dir="/vt/imp-1000/abc")
    argv = _argv("x", cwd="/wt", policy=pol, engine_env={})
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
    assert ("TMPDIR", expected) in pol.child_env


def test_var_tmp_source_not_identity_fallback():
    # An empty quest_var_tmp_dir must NEVER degrade to --bind /var/tmp /var/tmp
    # (which would expose the host's real /var/tmp read-write).
    flat = " ".join(_argv(
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
    # so build_sandbox_spawn picks them up with no edit to build_sandbox_spawn.
    p = _policy(
        git_worktrees_mask="/ctl/.git/worktrees",
        git_worktree_self="/ctl/.git/worktrees/abc",
    )
    argv = _argv("x", cwd="/wt", policy=p, engine_env={})
    assert _parse_argv_binds(argv) == p.bind_plan()
    assert ("/ctl/.git/worktrees", "mask_dir") in p.bind_plan()
    assert ("/ctl/.git/worktrees/abc", "rw") in p.bind_plan()


def _cross_project_policy(**over) -> SandboxPolicy:
    # Nested quest with $HOME writable: without the ro bind, other projects would be WRITABLE.
    base = dict(
        projects_ro="/home/u/.imp/projects",
        state_mask="/home/u/.imp/projects/mine",
        worktree="/home/u/.imp/projects/mine/worktrees/abc",
        quest_home="/home/u/.imp/projects/mine/quests/abc",
        home="/home/u",
        home_layer=HomeLayer(home="/home/u"),
    )
    base.update(over)
    return _policy(**base)


def test_other_projects_readable_never_writable_siblings_still_hidden():
    p = _cross_project_policy()
    other = Path("/home/u/.imp/projects/other/quests/zzz/events.jsonl")
    assert p.allows_read(other) is True
    assert p.allows_write(other) is False
    assert p.allows_read(
        Path("/home/u/.imp/projects/mine/quests/sib/events.jsonl")) is False
    assert p.allows_write(
        Path("/home/u/.imp/projects/mine/quests/abc/artifacts/x.md")) is True
    assert p.allows_write(
        Path("/home/u/.imp/projects/mine/worktrees/abc/src/x.py")) is True
    assert p.allows_write(Path("/home/u/notes.txt")) is True


def test_projects_ro_bind_sits_between_the_home_bind_and_the_state_tmpfs():
    p = _cross_project_policy()
    plan = p.bind_plan()
    paths = [b for b, _ in plan]
    assert ("/home/u/.imp/projects", "ro") in plan
    assert paths.index("/home/u") < paths.index("/home/u/.imp/projects")
    assert paths.index("/home/u/.imp/projects") < paths.index(
        "/home/u/.imp/projects/mine")
    assert paths.index("/home/u/.imp/projects/mine") < paths.index(
        "/home/u/.imp/projects/mine/worktrees/abc")
    assert paths.index("/home/u/.imp/projects/mine") < paths.index("/home/u/.ssh")


def test_draft_worktree_binds_before_the_projects_ro_bind():
    # Draft shape: worktree == control root, OUTSIDE the mask, so its rw bind is
    # emitted first and the whole state_block follows it.
    p = _cross_project_policy(worktree="/ctl")
    paths = [b for b, _ in p.bind_plan()]
    assert paths.index("/ctl") < paths.index("/home/u/.imp/projects")
    assert paths.index("/home/u/.imp/projects") < paths.index(
        "/home/u/.imp/projects/mine")
    assert paths.index("/home/u/.imp/projects/mine") < paths.index(
        "/home/u/.imp/projects/mine/quests/abc")


def test_a_draft_control_root_above_imp_home_cannot_write_other_projects():
    # A dotfiles-style control root at $HOME is an ANCESTOR of imp_home()/projects,
    # so its rw bind covers every other project. The state_block must follow it, or
    # last-wins hands the agent write access to every project's state.
    p = _cross_project_policy(worktree="/home/u")
    other = Path("/home/u/.imp/projects/other/quests/zzz/events.jsonl")
    assert p.allows_write(other) is False
    assert p.allows_read(other) is True
    assert p.allows_read(
        Path("/home/u/.imp/projects/mine/quests/sib/events.jsonl")) is False
    assert p.allows_write(Path("/home/u/src/x.py")) is True


def test_argv_binds_match_bind_plan_with_projects_ro():
    # Drift guard for all three worktree shapes: an ro bind that does not round-trip
    # through the argv parser means allows_read/allows_write lie about bwrap.
    for worktree in ("/home/u/.imp/projects/mine/worktrees/abc", "/ctl", "/home/u"):
        p = _cross_project_policy(worktree=worktree)
        argv = _argv("x", cwd="/wt", policy=p, engine_env={})
        assert _parse_argv_binds(argv) == p.bind_plan()
        assert ("/home/u/.imp/projects", "ro") in p.bind_plan()


def test_the_repo_marker_is_re_exposed_read_only_through_the_state_tmpfs():
    # AC 21: the mask hides the whole state dir, so the marker the $IMP_STATE_DIR guard
    # reads has to come back on top of it — and read-only, since nothing in-sandbox may
    # repoint a checkout's ownership.
    p = _cross_project_policy(state_repo_ro="/home/u/.imp/projects/mine/repo")
    plan = p.bind_plan()
    paths = [b for b, _ in plan]
    assert ("/home/u/.imp/projects/mine/repo", "ro") in plan
    assert paths.index("/home/u/.imp/projects/mine") < paths.index(
        "/home/u/.imp/projects/mine/repo")
    assert paths.index("/home/u/.imp/projects/mine/repo") < paths.index("/home/u/.ssh")
    assert paths.index("/home/u/.imp/projects/mine/repo") < paths.index(
        "/run/user/1000")
    assert paths.index("/home/u/.imp/projects/mine/repo") < paths.index("/home/u/.netrc")
    # The in-process mirror the fs tools consult agrees: readable, never writable.
    assert p.allows_read(Path("/home/u/.imp/projects/mine/repo")) is True
    assert p.allows_write(Path("/home/u/.imp/projects/mine/repo")) is False
    # The re-expose is ONE file: a sibling quest under the same mask stays hidden.
    assert p.allows_read(
        Path("/home/u/.imp/projects/mine/quests/sib/quest.json")) is False


def test_the_repo_re_expose_follows_the_mask_in_the_draft_shape():
    # Draft agent: worktree == control root, OUTSIDE the mask, so the whole state_block
    # is emitted after the worktree rw bind. The marker must still land after the tmpfs.
    p = _cross_project_policy(worktree="/ctl",
                              state_repo_ro="/home/u/.imp/projects/mine/repo")
    paths = [b for b, _ in p.bind_plan()]
    assert paths.index("/ctl") < paths.index("/home/u/.imp/projects/mine")
    assert paths.index("/home/u/.imp/projects/mine") < paths.index(
        "/home/u/.imp/projects/mine/repo")
    assert p.allows_read(Path("/home/u/.imp/projects/mine/repo")) is True


def test_no_repo_bind_when_the_field_is_empty():
    # A hand-pinned $IMP_STATE_DIR carries no marker; an unset field must emit nothing
    # at all, or bwrap aborts setup on a missing --ro-bind source.
    p = _cross_project_policy()
    assert p.state_repo_ro == ""
    assert not [b for b, _ in p.bind_plan() if b.endswith("/repo")]
    assert p.allows_read(Path("/home/u/.imp/projects/mine/repo")) is False


def test_argv_binds_match_bind_plan_with_the_repo_re_expose():
    # Drift guard for all three worktree shapes: an ro bind that does not round-trip
    # through the argv parser means allows_read/allows_write lie about bwrap.
    for worktree in ("/home/u/.imp/projects/mine/worktrees/abc", "/ctl", "/home/u"):
        p = _cross_project_policy(worktree=worktree,
                                  state_repo_ro="/home/u/.imp/projects/mine/repo")
        argv = _argv("x", cwd="/wt", policy=p, engine_env={})
        assert _parse_argv_binds(argv) == p.bind_plan()
        assert ("--ro-bind /home/u/.imp/projects/mine/repo "
                "/home/u/.imp/projects/mine/repo") in " ".join(argv)


def test_the_bind_plan_invariant_names_the_repo_re_expose():
    # AC 25(b): the exposed-set invariant is the contract every future bind edit is
    # read against, so it must account for the one path the mask no longer hides.
    doc = SandboxPolicy.bind_plan.__doc__ or ""
    invariant = doc[doc.index("Invariant:"):]
    assert "state_repo_ro" in invariant


def test_retired_overlay_symbols_are_gone():
    import imp.engine.sandbox as sb
    for name in ("OVERLAY_SETUP_FAILED_MARKER", "OVERLAY_SETUP_FAILED_CODE",
                 "OVERLAY_SETUP_FAILED_MESSAGE", "overlay_setup_failed"):
        assert not hasattr(sb, name), f"{name} should be deleted"


def test_retired_per_call_sentinel_machinery_is_gone():
    # AC 14a/b: the deletions are real, not merely unused. hasattr on the MODULE
    # is the call-site check — these names reach bg.py only by import, so an
    # import surviving means a call site survived with it.
    import imp.agents.tools.bg as bg
    for name in ("_detect_overlay_setup_failure", "_emit_overlay_setup_warning",
                 "strip_sandbox_marker", "sandbox_spawn_failed",
                 "sandbox_failure_detail", "SANDBOX_SPAWN_FAILED_MESSAGE"):
        assert not hasattr(bg, name), f"{name} should be gone from bg.py"
    for field in ("overlay_setup_failed", "overlay_reported", "sandbox_active"):
        assert field not in bg.BgProc.__dataclass_fields__
    # AC 14b's bash.py and executor.py halves. Named individually rather than
    # checked wholesale because both modules KEEP importing sandbox symbols that
    # are not part of the sentinel path: SANDBOX_SPAWN_MAX_ATTEMPTS (the retry
    # wording) in executor.py, and SANDBOX_SPAWN_FAILED_MESSAGE in bash.py.
    import imp.agents.tools.bash as bash
    import imp.engine.executor as ex
    for mod in (bash, ex):
        for name in ("strip_sandbox_marker", "sandbox_spawn_failed",
                     "sandbox_failure_detail"):
            assert not hasattr(mod, name), f"{name} should be gone from {mod.__name__}"
    # strip_sandbox_marker is gone OUTRIGHT: the executor's was its last call site,
    # and open()'s handshake reads the sentinel with _await_sentinel, never this.
    import imp.engine.sandbox as sb
    assert not hasattr(sb, "strip_sandbox_marker")
    # ...but the two the handshake DOES use must survive.
    assert sb.sandbox_spawn_failed("", sandbox_active=True) is True
    assert sb.sandbox_failure_detail("boom\nrest") == "boom"


def _flat_doc(obj: object) -> str:
    return " ".join((obj.__doc__ or "").split())


def test_the_overlay_and_keeper_docstrings_drop_their_retired_rationale():
    # Spec §8. Four docstrings explained a PER-SPAWN overlay layer and a blanket
    # --die-with-parent rejection; the keeper contradicts both, and a reader who
    # trusts either writes the wrong code. Flattened, because every asserted
    # sentence is hard-wrapped where it lives.
    import imp.agents.subagents as sa
    import imp.engine.executor as ex
    import imp.engine.sandbox as sb
    module_doc = _flat_doc(sb)
    assert "group reaping still reaches inside" not in module_doc
    assert "No `--die-with-parent` / `--new-session`" not in module_doc
    assert "`--die-with-parent` goes on the KEEPER" in module_doc
    assert "It must NOT go on the per-call join" in module_doc
    assert "The command travels in `$IMP_COMMAND` and the cwd in `$IMP_CWD`" in module_doc
    assert "never `--wd` or Popen's `cwd=`" in module_doc
    prepare_doc = _flat_doc(sb.prepare_spawn_policy)
    assert "Minting is per SPAWN" not in prepare_doc
    assert "minted once per namespace, not per spawn" in prepare_doc
    for fn in (sa._attach_isolated_home, ex._attach_home_layer):
        doc = _flat_doc(fn)
        assert "each spawn mints its own layer" not in doc
        assert "keeper namespace mints one layer" in doc


def test_normalize_returncode_separates_imps_kills_from_the_commands_own():
    # Spec §7. A join propagates a self-inflicted signal as a NEGATIVE code where
    # bwrap mapped it to 128+n; providers key tool_killed on the sign, so the two
    # must not blur. imp knows which it did, so the read site decides.
    from imp.engine.sandbox import normalize_returncode
    assert normalize_returncode(-9, imp_killed=False) == 137    # kill -9 $$
    assert normalize_returncode(-15, imp_killed=False) == 143   # kill -15 $$
    assert normalize_returncode(-15, imp_killed=True) == -15    # imp killed the group
    assert normalize_returncode(-9, imp_killed=True) == -9
    assert normalize_returncode(7, imp_killed=False) == 7       # untouched
    assert normalize_returncode(0, imp_killed=True) == 0
    assert normalize_returncode(None, imp_killed=False) is None


def test_child_env_carries_the_state_dir_when_the_spawn_supplies_none():
    # The bash tool passes no control root, so its live engine_env has no
    # IMP_STATE_DIR — the value baked into policy.child_env at run start must still
    # reach the sandboxed child.
    p = _policy(child_env=(("PATH", "/usr/bin"),
                           ("IMP_STATE_DIR", "/home/u/.imp/projects/widget")))
    spawn = build_sandbox_spawn("x", cwd="/wt", policy=p, engine_env={}, environ={})
    assert spawn.env["IMP_STATE_DIR"] == "/home/u/.imp/projects/widget"


def test_resolve_policy_masks_only_this_projects_state_dir(tmp_path):
    control, wt = _layout(tmp_path)
    pol = resolve_policy(
        worktree=wt, control_root=control, quest_id="abc",
        cfg=SandboxConfig(), engine_env_result={}, environ={}, available=True,
    )
    assert pol.state_mask == str(project_state_dir(control).resolve())
    assert (pol.state_mask, "mask_dir") in pol.bind_plan()
    assert pol.projects_ro == str((imp_home() / "projects").resolve())
    assert (pol.projects_ro, "ro") in pol.bind_plan()


def test_a_real_quests_worktree_is_nested_in_the_mask(tmp_path):
    # bind_plan's ordering turns on this: a false `nested` emits the tmpfs AFTER
    # the worktree rw bind and masks the agent's own worktree away. The mask is
    # resolved, the worktree is resolved, so a symlinked home cannot split them.
    control, wt = _layout(tmp_path)
    pol = resolve_policy(
        worktree=wt.resolve(), control_root=control, quest_id="abc",
        cfg=SandboxConfig(), engine_env_result={}, environ={}, available=True,
    )
    paths = [b for b, _ in pol.bind_plan()]
    assert paths.index(pol.state_mask) < paths.index(str(wt.resolve()))


def test_bind_plan_re_exposes_the_chdir_target_after_the_state_mask(tmp_path):
    # Pins the production failure: a --chdir target the plan never re-exposed.
    control, wt = _layout(tmp_path)
    quest_dir = project_state_dir(control) / "quests" / "abc"
    quest_dir.mkdir(parents=True)
    pol = resolve_policy(
        worktree=wt, control_root=control, quest_id="abc",
        cfg=SandboxConfig(), engine_env_result={}, environ={}, available=True,
        quest_dir=quest_dir,
    )
    plan = pol.bind_plan()
    assert pol.worktree == str(wt)
    assert (plan.index((pol.state_mask, "mask_dir"))
            < plan.index((pol.worktree, "rw")))

    argv = _argv("x", cwd=str(wt), policy=pol, engine_env={})
    chdir = argv[argv.index("--chdir") + 1]
    assert chdir == str(wt)
    assert pol.allows_read(Path(chdir)) is True
    assert pol.allows_write(Path(chdir)) is True


def test_a_symlinked_state_path_still_exposes_the_quests_own_home(
    tmp_path, monkeypatch,
):
    # The mask and the quest home must be resolved consistently: if the mask
    # resolves and the home does not, the policy mirror finds the mask covering
    # and the re-expose not covering, denying the agent its own artifacts.
    real = tmp_path / "real-home"
    real.mkdir()
    link = tmp_path / "linked-home"
    link.symlink_to(real)
    monkeypatch.setenv("IMP_HOME", str(link))
    control, wt = _layout(tmp_path)
    quest_dir = project_state_dir(control) / "quests" / "abc"
    quest_dir.mkdir(parents=True)
    pol = resolve_policy(
        worktree=wt.resolve(), control_root=control, quest_id="abc",
        cfg=SandboxConfig(), engine_env_result={}, environ={}, available=True,
        quest_dir=quest_dir,
    )
    artifact = quest_dir.resolve() / "artifacts" / "spec" / "abc.md"
    assert pol.allows_read(artifact) is True
    assert pol.allows_write(artifact) is True
    assert pol.allows_read(
        quest_dir.resolve().parent / "sibling" / "quest.json") is False


def test_other_projects_are_readable_but_never_writable(tmp_path):
    control, wt = _layout(tmp_path)
    quest_dir = project_state_dir(control) / "quests" / "abc"
    quest_dir.mkdir(parents=True)
    pol = resolve_policy(
        worktree=wt, control_root=control, quest_id="abc",
        cfg=SandboxConfig(), engine_env_result={}, environ={}, available=True,
        quest_dir=quest_dir,
    )
    other = imp_home() / "projects" / "other-repo" / "quests" / "zzz"
    other.mkdir(parents=True)
    assert pol.allows_read(other.resolve()) is True
    assert pol.allows_write(other.resolve() / "events.jsonl") is False
    assert pol.allows_read(
        quest_dir.resolve().parent / "sib" / "quest.json") is False
    assert pol.allows_read((imp_home() / "config.yaml").resolve()) is True
    assert pol.allows_read(wt.resolve() / "src" / "x.py") is True
    assert pol.allows_read(
        quest_dir.resolve() / "artifacts" / "spec" / "abc.md") is True


def test_state_mask_follows_the_override(tmp_path, monkeypatch):
    control = tmp_path / "ctl"
    (control / ".git" / "hooks").mkdir(parents=True)
    (control / ".git" / "config").write_text("")
    override = tmp_path / "elsewhere"
    monkeypatch.setenv("IMP_STATE_DIR", str(override))
    wt = (override / "worktrees" / "abc")
    wt.mkdir(parents=True)
    wt = wt.resolve()          # production always hands resolve_policy a resolved worktree
    pol = resolve_policy(
        worktree=wt, control_root=control, quest_id="abc",
        cfg=SandboxConfig(), engine_env_result={}, environ={}, available=True,
    )
    assert pol.state_mask == str(override.resolve())
    # imp_home()/projects does not exist under this fixture: the ro bind is then
    # skipped entirely — bwrap aborts setup on a missing bind source.
    assert pol.projects_ro == ""
    assert not [b for b, k in pol.bind_plan()
                if k == "ro" and b.endswith("/projects")]
    paths = [b for b, _ in pol.bind_plan()]
    # nested (AC 28's shape): mask first, worktree re-exposed on top
    assert paths.index(pol.state_mask) < paths.index(str(wt))


def test_the_override_is_masked_while_imp_home_projects_stays_readable(
    tmp_path, monkeypatch,
):
    control = tmp_path / "ctl"
    (control / ".git" / "hooks").mkdir(parents=True)
    (control / ".git" / "config").write_text("")
    other = imp_home() / "projects" / "other-repo"
    other.mkdir(parents=True)
    override = tmp_path / "elsewhere"
    monkeypatch.setenv("IMP_STATE_DIR", str(override))
    wt = override / "worktrees" / "abc"
    wt.mkdir(parents=True)
    wt = wt.resolve()          # production always hands resolve_policy a resolved worktree
    pol = resolve_policy(
        worktree=wt, control_root=control, quest_id="abc",
        cfg=SandboxConfig(), engine_env_result={}, environ={}, available=True,
    )
    # The ro bind never follows the override's arbitrary parent: it always names
    # imp_home()/projects, while the tmpfs covers the override directory itself.
    assert pol.projects_ro == str((imp_home() / "projects").resolve())
    assert pol.state_mask == str(override.resolve())
    assert pol.allows_read(other.resolve()) is True
    assert pol.allows_write(
        other.resolve() / "quests" / "zzz" / "events.jsonl") is False
    assert pol.allows_read(override.resolve() / "quests" / "sib") is False


def test_draft_ordering_holds_under_the_override(tmp_path, monkeypatch):
    # AC 32's second half: with the override set, the draft case (worktree ==
    # control root, outside the mask) still binds the worktree BEFORE the tmpfs
    # and re-exposes the quest home after it.
    control = tmp_path / "ctl"
    (control / ".git" / "hooks").mkdir(parents=True)
    (control / ".git" / "config").write_text("")
    override = tmp_path / "elsewhere"
    quest_dir = override / "quests" / "abc"
    quest_dir.mkdir(parents=True)
    monkeypatch.setenv("IMP_STATE_DIR", str(override))
    pol = resolve_policy(
        worktree=control.resolve(), control_root=control, quest_id="abc",
        cfg=SandboxConfig(), engine_env_result={}, environ={}, available=True,
        quest_dir=quest_dir,
    )
    paths = [b for b, _ in pol.bind_plan()]
    assert paths.index(str(control.resolve())) < paths.index(pol.state_mask) \
        < paths.index(str(quest_dir.resolve()))
    assert pol.allows_read(
        override.resolve() / "quests" / "sibling" / "state.json") is False
    assert pol.allows_read(
        quest_dir.resolve() / "artifacts" / "spec" / "abc.md") is True


def test_resolve_policy_re_exposes_this_projects_repo_marker(tmp_path):
    # AC 20: a policy for a REAL quest — the field must be wired at the one place
    # state_mask is set, not just constructible by hand.
    control, wt = _layout(tmp_path)
    pol = resolve_policy(
        worktree=wt, control_root=control, quest_id="abc",
        cfg=SandboxConfig(), engine_env_result={}, environ={}, available=True,
    )
    marker = project_state_dir(control).resolve() / "repo"
    assert pol.state_repo_ro == str(marker)
    assert pol.state_mask == str(marker.parent)
    assert (pol.state_repo_ro, "ro") in pol.bind_plan()
    # The slug walk already minted it, so the guard has a real file to read in-sandbox.
    assert marker.read_text() == f"{control.resolve()}\n"
    assert pol.allows_read(marker) is True
    assert pol.allows_write(marker) is False


def test_an_inactive_policy_carries_no_repo_re_expose(tmp_path):
    # AC 20's second half: the inactive policy binds nothing, so it must not claim a
    # path either — build_sandbox_spawn would never emit it, and the mirror must agree.
    control, _wt = _layout(tmp_path)
    pol = resolve_policy(
        worktree=control, control_root=control, quest_id="abc",
        cfg=SandboxConfig(allow_unsandboxed=True), engine_env_result={},
        environ={}, available=False,
    )
    assert pol.active is False
    assert pol.note == "unsandboxed_by_config"
    assert pol.state_repo_ro == ""
    assert not [b for b, _ in pol.bind_plan() if b.endswith("/repo")]
    assert _argv("x", cwd="/wt", policy=pol, engine_env={})[:2] == ["sh", "-c"]


def test_spawn_env_is_the_bake_refreshed_by_the_live_engine_env():
    # AC 2: the returned child env is the bake (policy.child_env) refreshed by the
    # live per-spawn engine env —
    # (a) IMP_QUEST_TITLE refreshed from the live per-spawn engine env (name-on-intent),
    # (b) the shim-prefixed PATH from the bake, (c) IMP_STATE_DIR retained from the bake
    # when the spawn's own engine env carries none. `environ` reaches an ACTIVE policy's
    # child not at all.
    from imp.engine.sandbox import _build_child_env
    baked = _build_child_env(
        {"IMP_QUEST_TITLE": "", "IMP_STATE_DIR": "/home/u/.imp/projects/widget"},
        {"PATH": "/usr/bin"}, (), shim_dir="/q/imp-shim")
    spawn = build_sandbox_spawn(
        "x", cwd="/wt", policy=_policy(child_env=baked),
        engine_env={"IMP_QUEST_TITLE": "Renamed quest",
                    "IMP_PROJECT_ROOT": "/state/worktrees/abc"},
        environ={"AMBIENT_SECRET": "hunter2"})
    assert spawn.env == {
        "PATH": "/q/imp-shim" + os.pathsep + "/usr/bin",
        "IMP_QUEST_TITLE": "Renamed quest",
        "IMP_STATE_DIR": "/home/u/.imp/projects/widget",
        "IMP_PROJECT_ROOT": "/state/worktrees/abc",
    }


def test_inactive_policy_spawn_keeps_the_host_environment():
    # AC 8: /sandbox off is deliberately unchanged — bare argv, the overlay_engine_env
    # result (ambient kept, engine-managed vars stripped then re-supplied) and the shim
    # PATH prefix.
    spawn = build_sandbox_spawn(
        "echo hi", cwd="/wt", policy=_policy(active=False),
        engine_env={"IMP_QUEST_ID": "abc"},
        environ={"PATH": "/usr/bin", "HOME": "/home/u",
                 "IMP_TEST_CMD_FAST": "stale-gate"},
        shim_dir="/q/imp-shim")
    assert spawn.argv[:2] == ["sh", "-c"]
    assert spawn.env == {
        "PATH": "/q/imp-shim" + os.pathsep + "/usr/bin",
        "HOME": "/home/u",
        "IMP_QUEST_ID": "abc",
        "IMP_COMMAND": "echo hi",
    }


def test_active_argv_carries_no_environment():
    # AC 1(a): argv is world-readable via /proc, so no --clearenv, no --setenv, and no
    # secret VALUE in any argv element — while the env is still delivered to the child.
    pol = _policy(child_env=(("PATH", "/usr/bin"),
                             ("IMP_TELEGRAM_TOKEN", "canary-42:secret")))
    spawn = build_sandbox_spawn("x", cwd="/wt", policy=pol,
                                engine_env={"IMP_MCP_TOKEN": "canary-mcp"},
                                environ={})
    assert "--clearenv" not in spawn.argv
    assert "--setenv" not in spawn.argv
    assert [a for a in spawn.argv if "canary" in a] == []
    assert spawn.env["IMP_TELEGRAM_TOKEN"] == "canary-42:secret"
    assert spawn.env["IMP_MCP_TOKEN"] == "canary-mcp"


def test_overlay_home_shell_string_carries_no_environment():
    # AC 1(b): the overlay-home shape re-joins the WHOLE argv into an
    # `unshare … sh -c <string>`; a secret on argv would leak through that string too.
    layer = HomeLayer(home="/home/u", merged="/ovl/merged", upper="/ovl/upper",
                      work="/ovl/work")
    pol = _policy(home="/home/u", home_layer=layer,
                  child_env=(("PATH", "/usr/bin"),
                             ("IMP_TELEGRAM_TOKEN", "canary-42:secret")))
    spawn = build_sandbox_spawn("x", cwd="/wt", policy=pol, engine_env={}, environ={})
    assert spawn.argv[0] == "unshare"
    assert "canary-42:secret" not in " ".join(spawn.argv)
    assert spawn.env["IMP_TELEGRAM_TOKEN"] == "canary-42:secret"


def _ns(*, network: bool = True, outer_user_fd: int | None = None) -> SandboxNamespace:
    """A namespace with DUMMY fd numbers: join() is pure given the pins, so its
    argv/env shape needs no real keeper. fds are 10.. in pin order."""
    kinds = ["user", "mnt", "pid", "uts", "ipc"] + ([] if network else ["net"])
    pol = _policy(network=network)
    return SandboxNamespace(
        policy=pol, base_policy=pol,
        ns_fds=tuple((k, 10 + i) for i, k in enumerate(kinds)),
        outer_user_fd=outer_user_fd, devpts_fd=99,
        ns_init_pid=4242, err_fd=98, proc=None)


def test_join_enumerates_the_namespace_flags_by_pinned_fd():
    argv = _ns().join("echo hi", cwd="/wt", engine_env={}).argv
    assert argv[0] == "nsenter"
    assert argv[1:6] == ["--user=/proc/self/fd/10", "--mount=/proc/self/fd/11",
                         "--pid=/proc/self/fd/12", "--uts=/proc/self/fd/13",
                         "--ipc=/proc/self/fd/14"]
    # mandatory: bwrap writes `deny` to setgroups, so without it nsenter dies
    assert "--preserve-credentials" in argv
    assert "--all" not in argv                                  # EPERM on net/cgroup/time
    assert not any(a.startswith("--target") for a in argv)      # pins, not a pid


def test_join_enters_the_net_namespace_iff_the_policy_isolates_it():
    # Fails OPEN if dropped: an omitted --net against an --unshare-net keeper
    # silently leaves the command in the HOST network namespace.
    isolated = _ns(network=False).join("x", cwd="/wt", engine_env={}).argv
    assert "--net=/proc/self/fd/15" in isolated
    networked = _ns(network=True).join("x", cwd="/wt", engine_env={}).argv
    assert not any(a.startswith("--net") for a in networked)


def test_join_passes_every_pinned_fd_to_the_child():
    spawn = _ns(network=False).join("x", cwd="/wt", engine_env={})
    assert spawn.pass_fds == (10, 11, 12, 13, 14, 15)


def test_join_keeps_command_and_cwd_off_argv():
    spawn = _ns().join('pkill -f "needle"', cwd="/masked/dir", engine_env={})
    joined = " ".join(spawn.argv)
    assert "pkill" not in joined and "needle" not in joined
    assert "/masked/dir" not in joined
    assert spawn.env["IMP_COMMAND"] == 'pkill -f "needle"'
    assert spawn.env["IMP_CWD"] == "/masked/dir"


def test_join_sets_the_cwd_with_an_inner_cd_and_never_die_with_parent():
    spawn = _ns().join("x", cwd="/masked/dir", engine_env={})
    # --wd resolves in the CALLER's mount namespace; the join must not use it,
    # and --die-with-parent here would collapse the runner's SIGTERM grace.
    assert "--wd" not in " ".join(spawn.argv)
    assert "--die-with-parent" not in spawn.argv
    assert spawn.argv[-2] == "-c"
    assert spawn.argv[-1].splitlines()[0] == 'cd "$IMP_CWD" || exit 1'
    assert 'unset IMP_COMMAND IMP_CWD' in spawn.argv[-1]


def test_join_drops_into_a_child_userns_before_running_the_command():
    # Entering the keeper's own user namespace grants CAP_FULL_SET there whenever
    # bwrap built one userns level, which is enough to umount a secret mask. The
    # child namespace owns nothing, so the stage must sit INSIDE the nsenter, right
    # ahead of the shell — anywhere earlier and the caps are regained on entry.
    argv = _ns().join("x", cwd="/wt", engine_env={}).argv
    assert argv[argv.index("--") + 1] == "unshare"
    assert argv[-7:-1] == ["unshare", "--user", "--map-current-user", "--",
                           "sh", "-c"]
    # --map-root-user would change the uid the command runs as; this one must not.
    assert "--map-root-user" not in argv
    # Two-stage shape: the placement requirement is SHARPER here, because between
    # the stages is a plausible-looking spot where the inner nsenter would hand the
    # caps straight back. Slicing past the outer `--` first is what makes the
    # assertion name the inner nsenter's `--` rather than the outer's.
    two_stage = _ns(outer_user_fd=77).join("x", cwd="/wt", engine_env={}).argv
    inner = two_stage[two_stage.index("--") + 1:]
    assert inner[inner.index("--") + 1] == "unshare"
    assert two_stage[-7:-1] == ["unshare", "--user", "--map-current-user", "--",
                                "sh", "-c"]


def test_join_env_overlays_the_live_engine_slice_on_the_policy_env():
    spawn = _ns().join("x", cwd="/wt", engine_env={
        "IMP_QUEST_TITLE": "renamed", "NOT_ENGINE": "1"})
    assert spawn.env["IMP_QUEST_TITLE"] == "renamed"   # name-on-intent override
    assert spawn.env["HOME"] == "/home/u"              # from policy.child_env
    assert "NOT_ENGINE" not in spawn.env


def test_join_enters_the_owner_userns_in_a_first_stage_when_bwrap_nested_two():
    # Two argv shapes, chosen at open() from a measured fact. When bwrap nests a
    # second userns for the payload (any imp uid != 0), ipc/uts/pid/mnt/net are owned
    # by the OUTER one and caps never flow up, so it must be entered first. With one
    # level OWNER == PAYLOAD, re-entering it is EINVAL -- hence None, and the
    # single-stage argv the tests above pin.
    spawn = _ns(outer_user_fd=77).join("echo hi", cwd="/masked/dir", engine_env={})
    argv = spawn.argv
    assert argv[:4] == ["nsenter", "--user=/proc/self/fd/77",
                        "--preserve-credentials", "--"]
    inner = argv[4:]
    assert inner[:6] == ["nsenter", "--user=/proc/self/fd/10",
                         "--mount=/proc/self/fd/11", "--pid=/proc/self/fd/12",
                         "--uts=/proc/self/fd/13", "--ipc=/proc/self/fd/14"]
    assert inner[6] == "--preserve-credentials"
    assert 77 in spawn.pass_fds
    joined = " ".join(argv)
    assert "echo hi" not in joined and "/masked/dir" not in joined


def test_join_on_a_closed_namespace_is_refused():
    # The pinned fd numbers are recycled after close(); an argv naming them would
    # silently enter WHATEVER now holds those numbers.
    ns = _ns()
    ns.closed = True
    with pytest.raises(AssertionError):
        ns.join("x", cwd="/wt", engine_env={})


def test_open_pty_on_a_closed_namespace_is_refused():
    # The devpts dirfd number is recycled after close(); allocating through it would
    # openat WHATEVER now holds that number.
    ns = _ns()
    ns.closed = True
    with pytest.raises(AssertionError):
        ns.open_pty()


def test_open_retries_the_whole_spawn_budget_and_reports_each_attempt(monkeypatch):
    attempts = []

    def boom(cls, **kw):
        attempts.append(kw)
        raise SandboxOpenFailed("overlay mount failed")

    monkeypatch.setattr(SandboxNamespace, "_open_once", classmethod(boom))
    monkeypatch.setattr(sandbox_mod, "sandbox_spawn_backoff_delay", lambda attempt: 0.0)
    seen: list[tuple[int, str]] = []
    with pytest.raises(SandboxOpenFailed) as exc:
        SandboxNamespace.open(policy=_policy(), engine_env={}, environ={},
                              on_retry=lambda n, detail: seen.append((n, detail)))
    assert len(attempts) == SANDBOX_SPAWN_MAX_ATTEMPTS
    # one breadcrumb per FAILED attempt, naming the attempt that follows it
    assert seen == [(n, "overlay mount failed")
                    for n in range(2, SANDBOX_SPAWN_MAX_ATTEMPTS + 1)]
    assert exc.value.detail == "overlay mount failed"


def test_open_stops_retrying_once_an_attempt_succeeds(monkeypatch):
    attempts: list[int] = []
    opened = object()

    def flaky(cls, **kw):
        attempts.append(1)
        if len(attempts) == 1:
            raise SandboxOpenFailed("transient")
        return opened

    monkeypatch.setattr(SandboxNamespace, "_open_once", classmethod(flaky))
    monkeypatch.setattr(sandbox_mod, "sandbox_spawn_backoff_delay", lambda attempt: 0.0)
    seen: list[tuple[int, str]] = []
    got = SandboxNamespace.open(policy=_policy(), engine_env={}, environ={},
                                on_retry=lambda n, detail: seen.append((n, detail)))
    assert got is opened
    assert len(attempts) == 2
    assert seen == [(2, "transient")]


def test_open_on_an_inactive_policy_is_refused():
    # A keeper built from an inactive policy would be a bare host `sh -c`, with
    # no namespaces to pin — fail closed rather than silently unsandbox the run.
    with pytest.raises(AssertionError):
        SandboxNamespace.open(policy=_policy(active=False), engine_env={}, environ={})


def test_nothing_is_bound_between_dev_and_chdir():
    # bwrap's --dev makes /dev/ptmx a SYMLINK into the devpts it mounts at
    # /dev/pts, so ANY bind landing on /dev/pts silently repoints ptmx at a
    # foreign instance. On a standard host that one is mounted ptmxmode=000 with
    # a ptmx owned by real root — unmapped in the sandbox's userns, so a payload's
    # CAP_DAC_OVERRIDE does not reach it and openpty() fails for EVERYTHING in the
    # sandbox (script, tmux, ssh -t). This is pure argv shape, so unlike the
    # behavioral guard it bites on a NESTED host too — i.e. in imp's own gates,
    # where the enclosing /dev/pts is already bwrap's permissive instance.
    dev_block = ["--proc", "/proc", "--dev", "/dev"]
    percall = _argv("echo hi", cwd="/state/worktrees/abc", policy=_policy(),
                    engine_env={})
    nonet = _argv("echo hi", cwd="/state/worktrees/abc",
                  policy=_policy(network=False), engine_env={})
    keeper = build_sandbox_spawn(KEEPER_PAYLOAD, cwd="/", policy=_policy(),
                                 engine_env={}, environ={}, keeper_info_fd=9).argv
    for argv in (percall, nonet, keeper):
        i = argv.index("--proc")
        assert argv[i:i + len(dev_block)] == dev_block
        assert argv[i + len(dev_block)] == "--chdir"
        # The invariant itself, not just this one slot: a bind emitted from
        # policy.bind_plan() (where every other bind in the argv comes from)
        # lands nowhere near --dev and would slip past the adjacency check.
        assert "/dev/pts" not in argv


def test_resolve_policy_reaches_the_quest_spike_dir(tmp_path):
    # AC 1(a): the ordinary agent policy permits read+write of <quest_home>/spike/
    # with no sandbox.py change — it rides the plain quest_home rw bind.
    control, wt = _layout(tmp_path)
    quest_dir = (project_state_dir(control) / "quests" / "abc")
    (quest_dir / "steps").mkdir(parents=True)
    pol = resolve_policy(worktree=wt, control_root=control, quest_id="abc",
                         cfg=SandboxConfig(), engine_env_result={}, environ={},
                         available=True, quest_dir=quest_dir)
    spike = quest_dir.resolve() / "spike"
    assert pol.allows_write(spike) is True
    assert pol.allows_read(spike / "probe.py") is True
    # steps/ stays masked — the reason spike/ is a sibling of it, not inside it.
    assert pol.allows_read(quest_dir.resolve() / "steps" / "s.jsonl") is False


def test_isolated_subagent_policy_reaches_the_quest_spike_dir(tmp_path, monkeypatch):
    # AC 1(b): the spiker runs as an isolated subagent, whose policy is built from
    # the same quest_dir — so its park path must be writable there too.
    import imp.agents.subagents as subagents

    control, wt = _layout(tmp_path)
    quest_dir = (project_state_dir(control) / "quests" / "abc")
    (quest_dir / "steps").mkdir(parents=True)
    monkeypatch.setattr("imp.providers.effective.load_effective_config",
                        lambda **k: _types.SimpleNamespace(
                            config=_types.SimpleNamespace(sandbox={}, pythonpath=())))
    monkeypatch.setattr("imp.engine.sandbox.sandbox_available", lambda: True)
    quest = _types.SimpleNamespace(project_root=wt, control_root=control, id="abc",
                                   title="t", quest_dir=quest_dir)

    pol = subagents._build_isolated_policy(quest, tmp_path / "spike-wt", None)

    spike = quest_dir.resolve() / "spike"
    assert pol.allows_write(spike) is True
    assert pol.allows_read(spike / "probe.py") is True
    assert pol.allows_read(quest_dir.resolve() / "steps" / "s.jsonl") is False
