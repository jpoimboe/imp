# tests/test_nav_tools.py
from __future__ import annotations

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
    wt = tmp_path / "wt"; wt.mkdir()
    ext = tmp_path / "ext"; ext.mkdir()
    (ext / "n.txt").write_text("needle here\n")
    ctx = _ctx(wt)                             # no sandbox_policy → external accepted
    out = _grep(ctx, pattern="needle", path=str(ext), output_mode="content")
    assert (ext / "n.txt").as_posix() in out   # absolute-form path in output


def test_grep_external_masked_target_denied_when_policy_active(tmp_path: Path) -> None:
    wt = tmp_path / "wt"; wt.mkdir()
    secret = tmp_path / "secret"; secret.mkdir()
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
    wt = tmp_path / "wt"; wt.mkdir()
    ext = tmp_path / "ext"; ext.mkdir()
    secret = tmp_path / "secret"; secret.mkdir()
    (secret / "id_rsa").write_text("")
    # symlink inside the allowed ext/ base pointing into the masked secret/ dir
    (ext / "link").symlink_to(secret / "id_rsa")
    (ext / "ok.txt").write_text("")
    ctx = _ctx(wt)
    ctx.sandbox_policy = _policy(wt, secret_dirs=(str(secret),))
    out = _glob(ctx, pattern="*", path=str(ext))
    assert "ok.txt" in out
    assert "link" not in out                   # symlink into masked dir must be dropped


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
