# tests/test_nav_tools.py
from __future__ import annotations

import subprocess
from pathlib import Path

import anyio
import pytest

import imp.agents.tools.nav  # noqa: F401
from imp.agents.tools import get_registry
from imp.agents.tools.context import ToolContext
from imp.agents.tools.limits import ToolAborted


class _Coord:
    def __init__(self, should_pause: bool = False) -> None:
        self.should_pause = should_pause


def _ctx(tmp_path: Path, coord: object | None = None) -> ToolContext:
    ctx = ToolContext(
        project_root=tmp_path,
        fs_permissions=(),
        events_path=tmp_path / "events.jsonl",
    )
    ctx.coordinator = coord
    return ctx


def _grep(ctx: ToolContext, **args: object) -> str:
    return anyio.run(get_registry()["grep"].async_invoke, ctx, args)


def _glob(ctx: ToolContext, **args: object) -> str:
    return anyio.run(get_registry()["glob"].async_invoke, ctx, args)


def test_glob_finds_files(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("")
    (tmp_path / "b.py").write_text("")
    (tmp_path / "c.txt").write_text("")
    out = _glob(_ctx(tmp_path), pattern="*.py")
    assert "a.py" in out
    assert "b.py" in out
    assert "c.txt" not in out


def test_glob_recursive(tmp_path: Path) -> None:
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "x.md").write_text("")
    out = _glob(_ctx(tmp_path), pattern="**/*.md")
    assert "sub/x.md" in out


def test_grep_finds_match(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("hello world\nfoo bar\n")
    (tmp_path / "b.py").write_text("nothing\n")
    out = _grep(_ctx(tmp_path), pattern="hello", path=".")
    assert "a.py" in out
    assert "b.py" not in out


def test_grep_excludes_imp_and_git(tmp_path: Path) -> None:
    (tmp_path / "code.py").write_text("SECRET token here\n")
    (tmp_path / ".imp").mkdir()
    (tmp_path / ".imp" / "events.jsonl").write_text("SECRET in the event log\n")
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "config").write_text("SECRET in git config\n")

    out = _grep(_ctx(tmp_path), pattern="SECRET", path=".")

    assert "code.py" in out
    assert ".imp" not in out
    assert ".git" not in out


def test_grep_bounds_a_single_giant_line(tmp_path: Path) -> None:
    # The incident shape: one enormous single line with no newline. Here the
    # operative bound is MATCH_LINE_CAP_BYTES (each matched line is byte-truncated)
    # — the 4 MB file is under FILE_READ_CAP_BYTES (5 MB) so it is read whole; the
    # *read* ceiling itself is unit-tested separately in test_tool_limits.py.
    big = tmp_path / "huge.log"
    big.write_bytes(b"NEEDLE" + b"x" * 4_000_000)  # one line, 4 MB, no newline
    out = _grep(_ctx(tmp_path), pattern="NEEDLE", output_mode="content")
    # The matched line is byte-truncated, not a 4 MB result.
    assert "NEEDLE" in out
    assert len(out) < 10_000


def test_grep_caps_total_output(tmp_path: Path) -> None:
    # Many matching lines -> output stops at the head cap with the marker.
    f = tmp_path / "many.txt"
    f.write_text("\n".join(f"match line {i}" for i in range(200_000)) + "\n")
    out = _grep(_ctx(tmp_path), pattern="match", output_mode="content")
    assert "[output truncated at 50 KB]" in out
    assert len(out) < 60_000


def test_grep_aborts_promptly_on_pause(tmp_path: Path) -> None:
    # C-i: should_pause set BEFORE the call -> aborts at the first checkpoint.
    for i in range(50):
        (tmp_path / f"f{i}.txt").write_text("hay\nNEEDLE\nhay\n")
    ctx = _ctx(tmp_path, coord=_Coord(should_pause=True))
    with pytest.raises(ToolAborted) as ei:
        _grep(ctx, pattern="NEEDLE")
    assert ei.value.reason == "interrupted"


def test_grep_still_finds_normal_matches(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("def foo():\n    pass\n")
    out = _grep(_ctx(tmp_path), pattern=r"def foo", output_mode="content")
    assert "a.py:1:def foo():" in out


def test_glob_excludes_imp_and_git(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("x")
    (tmp_path / ".imp").mkdir()
    (tmp_path / ".imp" / "events.jsonl").write_text("x")
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "config").write_text("x")

    out = _glob(_ctx(tmp_path), pattern="**/*")

    assert "a.py" in out
    assert ".imp" not in out
    assert ".git" not in out


def test_glob_in_worktree_subpath_is_root_relative(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "foo.py").write_text("")
    ctx = _ctx(tmp_path)
    out = _glob(ctx, pattern="*.py", path="src")
    assert out == "src/foo.py"                 # relative to root, not just "foo.py"


def test_glob_aborts_promptly_on_pause(tmp_path: Path) -> None:
    for i in range(50):
        (tmp_path / f"f{i}.txt").write_text("x")
    ctx = _ctx(tmp_path, coord=_Coord(should_pause=True))
    with pytest.raises(ToolAborted) as ei:
        _glob(ctx, pattern="**/*")
    assert ei.value.reason == "interrupted"


# --- Reachability-parity tests ---

from imp.agents.permissions import PermissionDenied  # noqa: E402
from imp.engine.sandbox import SandboxPolicy  # noqa: E402


def _policy(
    worktree: Path,
    *,
    secret_dirs: tuple[str, ...] = (),
    secret_files: tuple[str, ...] = (),
) -> SandboxPolicy:
    return SandboxPolicy(
        active=True,
        worktree=str(worktree),
        git_common_dir=str(worktree / ".git"),
        git_ro_remask=(),
        writable_caches=(),
        extra_readable=(),
        extra_writable=(),
        secret_dirs=secret_dirs,
        secret_files=secret_files,
        setenv=(),
        network=True,
        note="",
    )


def test_grep_external_accepted_when_policy_inactive(tmp_path: Path) -> None:
    wt = tmp_path / "wt"
    wt.mkdir()
    ext = tmp_path / "ext"
    ext.mkdir()
    (ext / "n.txt").write_text("needle here\n")
    ctx = _ctx(wt)                             # no sandbox_policy → external accepted
    out = _grep(ctx, pattern="needle", path=str(ext), output_mode="content")
    assert (ext / "n.txt").as_posix() in out   # absolute-form path in output


def test_grep_external_masked_target_denied_when_policy_active(tmp_path: Path) -> None:
    wt = tmp_path / "wt"
    wt.mkdir()
    secret = tmp_path / "secret"
    secret.mkdir()
    (secret / "id_rsa").write_text("KEY\n")
    ctx = _ctx(wt)
    ctx.sandbox_policy = _policy(wt, secret_dirs=(str(secret),))
    with pytest.raises(PermissionDenied):
        _grep(ctx, pattern="KEY", path=str(secret))


def test_glob_default_path_is_relative(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("")
    out = _glob(_ctx(tmp_path), pattern="*.py", path=".")
    assert out == "a.py"                       # byte-for-byte: relative, no prefix


def test_glob_external_path_absolute_output(tmp_path: Path) -> None:
    wt = tmp_path / "wt"
    wt.mkdir()
    ext = tmp_path / "ext"
    ext.mkdir()
    (ext / "a.py").write_text("")
    ctx = _ctx(wt)                             # no sandbox_policy → external accepted
    out = _glob(ctx, pattern="*.py", path=str(ext))
    assert out == (ext / "a.py").as_posix()    # absolute-form output for external base


def test_glob_external_masked_base_denied_when_policy_active(tmp_path: Path) -> None:
    wt = tmp_path / "wt"
    wt.mkdir()
    secret = tmp_path / "secret"
    secret.mkdir()
    (secret / "id_rsa").write_text("")
    ctx = _ctx(wt)
    ctx.sandbox_policy = _policy(wt, secret_dirs=(str(secret),))
    with pytest.raises(PermissionDenied):
        _glob(ctx, pattern="*", path=str(secret))


def test_glob_external_per_result_symlink_into_masked_dir_dropped(tmp_path: Path) -> None:
    wt = tmp_path / "wt"
    wt.mkdir()
    ext = tmp_path / "ext"
    ext.mkdir()
    secret = tmp_path / "secret"
    secret.mkdir()
    (secret / "id_rsa").write_text("")
    # symlink inside the allowed ext/ base pointing into the masked secret/ dir
    (ext / "link").symlink_to(secret / "id_rsa")
    (ext / "ok.txt").write_text("")
    ctx = _ctx(wt)
    ctx.sandbox_policy = _policy(wt, secret_dirs=(str(secret),))
    out = _glob(ctx, pattern="*", path=str(ext))
    assert "ok.txt" in out
    assert "link" not in out                   # symlink into masked dir must be dropped


def test_glob_inworktree_masked_file_dropped(tmp_path: Path) -> None:
    wt = tmp_path / "wt"
    wt.mkdir()
    (wt / "ok.txt").write_text("")
    (wt / "secret.txt").write_text("")
    ctx = _ctx(wt)
    ctx.sandbox_policy = _policy(wt, secret_files=(str(wt / "secret.txt"),))
    out = _glob(ctx, pattern="*.txt")
    assert "ok.txt" in out
    assert "secret.txt" not in out          # in-worktree masked file must be dropped


def test_glob_description_documents_path_and_sandbox() -> None:
    desc = get_registry()["glob"].description
    assert "path" in desc
    assert "bounded by the sandbox like bash" in desc


def test_grep_reports_relative_paths(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("needle here\n")
    ctx = _ctx(tmp_path)
    out = anyio.run(get_registry()["grep"].async_invoke, ctx,
                    {"pattern": "needle", "path": ".", "output_mode": "content"})
    assert out.startswith("a.txt:")          # relative, no /project prefix


def test_nav_module_docstring_drops_ls() -> None:
    import imp.agents.tools.nav as nav_mod
    assert "ls" not in (nav_mod.__doc__ or "")


def test_grep_external_walk_prunes_masked_dir(tmp_path: Path) -> None:
    wt = tmp_path / "wt"
    wt.mkdir()
    ext = tmp_path / "ext"
    ext.mkdir()
    (ext / "ok.txt").write_text("SECRET ok\n")
    sub = ext / "sshlike"
    sub.mkdir()
    (sub / "id_rsa").write_text("SECRET key\n")
    ctx = _ctx(wt)
    ctx.sandbox_policy = _policy(wt, secret_dirs=(str(sub),))
    out = _grep(ctx, pattern="SECRET", path=str(ext))
    assert (ext / "ok.txt").as_posix() in out
    assert "id_rsa" not in out                 # masked dir pruned, file never scanned


def test_grep_external_walk_symlink_into_masked_dir_dropped(tmp_path: Path) -> None:
    wt = tmp_path / "wt"
    wt.mkdir()
    ext = tmp_path / "ext"
    ext.mkdir()
    secret = tmp_path / "secret"
    secret.mkdir()
    (secret / "id_rsa").write_text("KEY\n")
    # symlink inside the allowed ext/ walk pointing into the masked secret/ dir
    (ext / "link").symlink_to(secret / "id_rsa")
    (ext / "ok.txt").write_text("KEY ok\n")
    ctx = _ctx(wt)
    ctx.sandbox_policy = _policy(wt, secret_dirs=(str(secret),))
    out = _grep(ctx, pattern="KEY", path=str(ext))
    assert (ext / "ok.txt").as_posix() in out
    assert "link" not in out                   # symlink into masked dir dropped


def test_grep_external_walk_cap_truncates(tmp_path: Path, monkeypatch) -> None:
    import imp.agents.tools.nav as nav_mod
    monkeypatch.setattr(nav_mod, "EXTERNAL_WALK_FILE_CAP", 3)
    wt = tmp_path / "wt"
    wt.mkdir()
    ext = tmp_path / "ext"
    ext.mkdir()
    for i in range(10):
        (ext / f"f{i}.txt").write_text("nomatch\n")
    out = _grep(_ctx(wt), pattern="ZZZ", path=str(ext))
    assert "external walk truncated" in out
    assert "scanned first 3 files" in out
    assert "(no matches)" not in out


def test_grep_inworktree_never_walk_capped(tmp_path: Path, monkeypatch) -> None:
    import imp.agents.tools.nav as nav_mod
    monkeypatch.setattr(nav_mod, "EXTERNAL_WALK_FILE_CAP", 3)
    for i in range(10):
        (tmp_path / f"f{i}.txt").write_text("nomatch\n")
    out = _grep(_ctx(tmp_path), pattern="ZZZ", path=".")
    assert "external walk truncated" not in out
    assert out == "(no matches)"


def test_grep_description_notes_sandbox_and_walk_cap() -> None:
    desc = get_registry()["grep"].description
    assert "bounded by the sandbox like bash" in desc
    assert "external" in desc and "cap" in desc


def test_glob_surfaces_artifact_subtree_but_not_rest_of_imp(tmp_path: Path) -> None:
    plans = tmp_path / ".imp" / "changes" / "c1" / "artifacts" / "plans"
    plans.mkdir(parents=True)
    (plans / "feat-plan-1.md").write_text("# plan 1\n")
    (plans / "feat-plan-2.md").write_text("# plan 2\n")
    (tmp_path / ".imp" / "events.jsonl").write_text("runtime log\n")  # must stay hidden

    out = _glob(_ctx(tmp_path),
                pattern=".imp/changes/c1/artifacts/plans/feat-plan-*.md")
    assert ".imp/changes/c1/artifacts/plans/feat-plan-1.md" in out
    assert ".imp/changes/c1/artifacts/plans/feat-plan-2.md" in out

    # A wildcard sweep surfaces the artifacts but never the runtime files.
    out_all = _glob(_ctx(tmp_path), pattern="**/*")
    assert ".imp/changes/c1/artifacts/plans/feat-plan-1.md" in out_all
    assert "events.jsonl" not in out_all


def test_grep_surfaces_artifact_subtree_but_not_rest_of_imp(tmp_path: Path) -> None:
    art = tmp_path / ".imp" / "changes" / "c1" / "artifacts" / "spec"
    art.mkdir(parents=True)
    (art / "feat.md").write_text("the DECISION is X\n")
    # A runtime file with the same needle: it must remain excluded.
    (tmp_path / ".imp" / "events.jsonl").write_text("the DECISION leaked\n")

    out = _grep(_ctx(tmp_path), pattern="DECISION", path=".")
    assert ".imp/changes/c1/artifacts/spec/feat.md" in out
    assert "events.jsonl" not in out


# --- gitignore-aware grep tests ---

def _init_git_repo(root: Path, gitignore: str) -> None:
    def g(*a: str) -> None:
        subprocess.run(["git", *a], cwd=root, capture_output=True,
                       text=True, check=True)
    g("init", "-q", "-b", "main")
    g("config", "user.email", "t@t")
    g("config", "user.name", "t")
    (root / ".gitignore").write_text(gitignore)
    (root / "README.md").write_text("seed\n")
    g("add", "-A")
    g("commit", "-qm", "seed")


def test_grep_skips_ignored_dirs_hidden_and_nonhidden(tmp_path: Path) -> None:
    _init_git_repo(tmp_path,
                   ".mypy_cache/\nbuild/\n__pycache__/\n_bmad-output/\n")
    (tmp_path / "real.py").write_text("NEEDLE here\n")
    for d in (".mypy_cache", "build", "__pycache__", "_bmad-output"):
        (tmp_path / d).mkdir()
        (tmp_path / d / "junk.py").write_text("NEEDLE junk\n")

    out = _grep(_ctx(tmp_path), pattern="NEEDLE", path=".")
    assert "real.py" in out
    assert ".mypy_cache" not in out
    assert "build" not in out
    assert "__pycache__" not in out
    assert "_bmad-output" not in out


def test_grep_searches_tracked_hidden_dir(tmp_path: Path) -> None:
    _init_git_repo(tmp_path, "build/\n")
    gh = tmp_path / ".github"
    gh.mkdir()
    (gh / "ci.yml").write_text("NEEDLE in ci\n")
    subprocess.run(["git", "add", "-A"], cwd=tmp_path,
                   capture_output=True, text=True, check=True)
    subprocess.run(["git", "commit", "-qm", "add gh"], cwd=tmp_path,
                   capture_output=True, text=True, check=True)

    out = _grep(_ctx(tmp_path), pattern="NEEDLE", path=".")
    assert ".github/ci.yml" in out


def test_grep_searches_fresh_untracked_nonignored_file(tmp_path: Path) -> None:
    _init_git_repo(tmp_path, "build/\n")
    (tmp_path / "brand_new.py").write_text("NEEDLE fresh\n")
    out = _grep(_ctx(tmp_path), pattern="NEEDLE", path=".")
    assert "brand_new.py" in out


def test_grep_skips_individually_ignored_file(tmp_path: Path) -> None:
    _init_git_repo(tmp_path, "secret.txt\n")
    (tmp_path / "secret.txt").write_text("NEEDLE secret\n")
    (tmp_path / "ok.txt").write_text("NEEDLE ok\n")
    out = _grep(_ctx(tmp_path), pattern="NEEDLE", path=".")
    assert "ok.txt" in out
    assert "secret.txt" not in out


def test_grep_escape_hatch_searches_explicit_ignored_dir(tmp_path: Path) -> None:
    _init_git_repo(tmp_path, "build/\n")
    (tmp_path / "build").mkdir()
    (tmp_path / "build" / "artifact.txt").write_text("NEEDLE inside build\n")
    out = _grep(_ctx(tmp_path), pattern="NEEDLE", path="build")
    assert "build/artifact.txt" in out


def test_grep_carveout_searched_in_repo(tmp_path: Path) -> None:
    _init_git_repo(tmp_path, ".imp/\n")
    art = tmp_path / ".imp" / "changes" / "c1" / "artifacts" / "spec"
    art.mkdir(parents=True)
    (art / "feat.md").write_text("the DECISION is X\n")
    (tmp_path / ".imp" / "events.jsonl").write_text("the DECISION leaked\n")

    out = _grep(_ctx(tmp_path), pattern="DECISION", path=".")
    assert ".imp/changes/c1/artifacts/spec/feat.md" in out
    assert "events.jsonl" not in out


def test_grep_nested_gitignore_honored(tmp_path: Path) -> None:
    _init_git_repo(tmp_path, "build/\n")
    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / ".gitignore").write_text("*.tmp\n")
    (sub / "keep.py").write_text("NEEDLE keep\n")
    (sub / "drop.tmp").write_text("NEEDLE drop\n")

    out = _grep(_ctx(tmp_path), pattern="NEEDLE", path=".")
    assert "sub/keep.py" in out
    assert "drop.tmp" not in out


def test_grep_git_failure_degrades_to_fallback(tmp_path: Path, monkeypatch) -> None:
    import imp.agents.tools.nav as nav_mod

    async def _fail(*a: object, **k: object) -> None:
        return None
    monkeypatch.setattr(nav_mod, "list_unignored", _fail)

    _init_git_repo(tmp_path, "build/\n")
    (tmp_path / "build").mkdir()
    # build/ is gitignored: the normal git path would skip it. With
    # list_unignored failing, the os.walk fallback (EXCLUDED_DIRS only, no
    # gitignore) searches it instead — proving the degrade.
    (tmp_path / "build" / "x.py").write_text("NEEDLE in build\n")
    out = _grep(_ctx(tmp_path), pattern="NEEDLE", path=".")
    assert "build/x.py" in out


def test_grep_skips_binary_file_with_nul_no_repo(tmp_path: Path) -> None:
    (tmp_path / "bin.dat").write_bytes(b"NEEDLE\x00more bytes\n")
    (tmp_path / "text.txt").write_text("NEEDLE in text\n")
    out = _grep(_ctx(tmp_path), pattern="NEEDLE", path=".")
    assert "text.txt" in out
    assert "bin.dat" not in out


def test_grep_skips_tracked_binary_in_repo(tmp_path: Path) -> None:
    _init_git_repo(tmp_path, "build/\n")
    (tmp_path / "img.bin").write_bytes(b"NEEDLE\x00\x01\x02payload\n")
    (tmp_path / "src.txt").write_text("NEEDLE source\n")
    subprocess.run(["git", "add", "-A"], cwd=tmp_path,
                   capture_output=True, text=True, check=True)
    subprocess.run(["git", "commit", "-qm", "add binary"], cwd=tmp_path,
                   capture_output=True, text=True, check=True)

    out = _grep(_ctx(tmp_path), pattern="NEEDLE", path=".")
    assert "src.txt" in out
    assert "img.bin" not in out


def test_grep_binary_skip_on_external_target(tmp_path: Path) -> None:
    wt = tmp_path / "wt"
    wt.mkdir()
    ext = tmp_path / "ext"
    ext.mkdir()
    (ext / "bin.dat").write_bytes(b"NEEDLE\x00rest\n")
    (ext / "text.txt").write_text("NEEDLE text\n")
    out = _grep(_ctx(wt), pattern="NEEDLE", path=str(ext))
    assert (ext / "text.txt").as_posix() in out
    assert "bin.dat" not in out


# --- gitignore-aware glob tests ---

def test_glob_skips_ignored_dirs(tmp_path: Path) -> None:
    _init_git_repo(tmp_path, ".mypy_cache/\nbuild/\n")
    (tmp_path / "real.py").write_text("")
    for d in (".mypy_cache", "build"):
        (tmp_path / d).mkdir()
        (tmp_path / d / "junk.py").write_text("")

    out = _glob(_ctx(tmp_path), pattern="**/*.py")
    assert "real.py" in out
    assert "junk.py" not in out


def test_glob_searches_tracked_hidden_dir(tmp_path: Path) -> None:
    _init_git_repo(tmp_path, "build/\n")
    gh = tmp_path / ".github"
    gh.mkdir()
    (gh / "ci.yml").write_text("")
    subprocess.run(["git", "add", "-A"], cwd=tmp_path,
                   capture_output=True, text=True, check=True)
    subprocess.run(["git", "commit", "-qm", "gh"], cwd=tmp_path,
                   capture_output=True, text=True, check=True)

    out = _glob(_ctx(tmp_path), pattern="**/*.yml")
    assert ".github/ci.yml" in out


def test_glob_keeps_fresh_untracked_nonignored_file(tmp_path: Path) -> None:
    _init_git_repo(tmp_path, "build/\n")
    (tmp_path / "fresh.py").write_text("")
    out = _glob(_ctx(tmp_path), pattern="*.py")
    assert "fresh.py" in out


def test_glob_carveout_kept_in_repo(tmp_path: Path) -> None:
    _init_git_repo(tmp_path, ".imp/\n")
    plans = tmp_path / ".imp" / "changes" / "c1" / "artifacts" / "plans"
    plans.mkdir(parents=True)
    (plans / "feat-plan-1.md").write_text("")
    (tmp_path / ".imp" / "events.jsonl").write_text("")

    out = _glob(_ctx(tmp_path), pattern="**/*")
    assert ".imp/changes/c1/artifacts/plans/feat-plan-1.md" in out
    assert "events.jsonl" not in out


def test_glob_escape_hatch_explicit_ignored_base(tmp_path: Path) -> None:
    _init_git_repo(tmp_path, "build/\n")
    (tmp_path / "build").mkdir()
    (tmp_path / "build" / "out.o").write_text("")
    out = _glob(_ctx(tmp_path), pattern="*.o", path="build")
    assert "build/out.o" in out


def test_glob_description_documents_gitignore_skip() -> None:
    desc = get_registry()["glob"].description
    assert "git ignores" in desc
    assert ".imp/changes/<id>/artifacts/" in desc
    assert "searches it anyway" in desc
    # retained guarantees
    assert "bounded by the sandbox like bash" in desc


def test_grep_description_documents_gitignore_and_binary() -> None:
    desc = get_registry()["grep"].description
    assert "git ignores" in desc
    assert ".imp/changes/<id>/artifacts/" in desc
    assert "searches it anyway" in desc
    assert "binary" in desc
    # retained guarantees
    assert "bounded by the sandbox like bash" in desc
    assert "external" in desc and "cap" in desc


def test_glob_git_failure_degrades_to_no_gitignore_filter(tmp_path: Path, monkeypatch) -> None:
    import imp.agents.tools.nav as nav_mod

    async def _fail(*a: object, **k: object) -> None:
        return None
    monkeypatch.setattr(nav_mod, "partition_ignored", _fail)

    _init_git_repo(tmp_path, "build/\n")
    (tmp_path / "build").mkdir()
    (tmp_path / "build" / "out.py").write_text("")
    (tmp_path / "ok.py").write_text("")

    out = _glob(_ctx(tmp_path), pattern="**/*.py")
    # With partition_ignored failing, ignored build/ is NOT filtered
    assert "ok.py" in out
    assert "build/out.py" in out


def test_grep_no_repo_fallback_uses_excluded_dirs_not_gitignore(tmp_path: Path) -> None:
    # No git repo: grep falls back to os.walk + EXCLUDED_DIRS ({".imp", ".git"}).
    # A directory that git would normally ignore (build/) is still searched.
    (tmp_path / "real.py").write_text("NEEDLE here\n")
    (tmp_path / "build").mkdir()
    (tmp_path / "build" / "out.py").write_text("NEEDLE in build\n")
    out = _grep(_ctx(tmp_path), pattern="NEEDLE", path=".")
    assert "real.py" in out
    assert "build/out.py" in out   # not ignored — fallback uses EXCLUDED_DIRS only


def test_grep_git_path_respects_sandbox_masking(tmp_path: Path) -> None:
    _init_git_repo(tmp_path, "build/\n")
    (tmp_path / "ok.py").write_text("NEEDLE ok\n")
    (tmp_path / "secret.py").write_text("NEEDLE secret\n")
    subprocess.run(["git", "add", "-A"], cwd=tmp_path,
                   capture_output=True, text=True, check=True)
    subprocess.run(["git", "commit", "-qm", "add files"], cwd=tmp_path,
                   capture_output=True, text=True, check=True)
    ctx = _ctx(tmp_path)
    ctx.sandbox_policy = _policy(tmp_path, secret_files=(str(tmp_path / "secret.py"),))
    out = _grep(ctx, pattern="NEEDLE", path=".")
    assert "ok.py" in out
    assert "secret.py" not in out


def test_grep_binary_skip_on_escape_hatch_path(tmp_path: Path) -> None:
    _init_git_repo(tmp_path, "build/\n")
    (tmp_path / "build").mkdir()
    (tmp_path / "build" / "text.txt").write_text("NEEDLE text\n")
    (tmp_path / "build" / "bin.dat").write_bytes(b"NEEDLE\x00binary content\n")
    # Explicitly targeting ignored build/ triggers the escape hatch (plain os.walk).
    out = _grep(_ctx(tmp_path), pattern="NEEDLE", path="build")
    assert "build/text.txt" in out
    assert "bin.dat" not in out


def test_glob_surfaces_sanctioned_change_home_symlink(tmp_path: Path) -> None:
    root = tmp_path / "wt"
    root.mkdir()
    _init_git_repo(root, ".imp/\n")
    # Out-of-worktree permanent home, reached via the sanctioned symlink.
    plans = tmp_path / "home" / "artifacts" / "plans"
    plans.mkdir(parents=True)
    (plans / "c1-plan-1.md").write_text("# plan 1\n")
    changes = root / ".imp" / "changes"
    changes.mkdir(parents=True)
    (changes / "c1").symlink_to(tmp_path / "home")

    out = _glob(_ctx(root),
                pattern=".imp/changes/c1/artifacts/plans/*.md")
    assert ".imp/changes/c1/artifacts/plans/c1-plan-1.md" in out


def test_glob_drops_nonsanctioned_escape_symlink(tmp_path: Path) -> None:
    root = tmp_path / "wt"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("")
    (root / "keep.txt").write_text("")
    (root / "link").symlink_to(outside)        # escapes root, NOT under artifacts/

    out = _glob(_ctx(root), pattern="*")
    assert "keep.txt" in out
    assert "link" not in out                    # escape guard still drops it
