"""The symlink-safe saved-file reader over a live quest home's artifacts/."""
from __future__ import annotations

import os
import re
import shutil
import threading
from pathlib import Path

from imp.engine import artifact_files
from imp.paths import project_state_dir
from tests.conftest import _init_repo


def _fixture(tmp_path: Path) -> Path:
    """A quest home whose artifacts/ holds two regular files plus links to outside."""
    outside = tmp_path / "outside"
    (outside / "dir").mkdir(parents=True)
    (outside / "secret.md").write_text("SECRET\n")
    (outside / "dir" / "inner.md").write_text("INNER\n")
    home = tmp_path / "home"
    art = home / "artifacts"
    (art / "spec").mkdir(parents=True)
    (art / "spec" / "q.md").write_text("# spec\n")
    (art / "top.md").write_text("top\n")
    (art / "leak.md").symlink_to(outside / "secret.md")
    (art / "linkdir").symlink_to(outside / "dir")
    return home


def test_list_files_keeps_only_regular_files(tmp_path: Path) -> None:
    home = _fixture(tmp_path)
    assert artifact_files.list_files(home) == ["spec/q.md", "top.md"]


def test_read_file_returns_a_regular_file(tmp_path: Path) -> None:
    home = _fixture(tmp_path)
    assert artifact_files.read_file(home, "spec/q.md") == b"# spec\n"
    assert artifact_files.read_file(home, "top.md") == b"top\n"


def test_read_file_refuses_a_folder(tmp_path: Path) -> None:
    home = _fixture(tmp_path)
    assert artifact_files.read_file(home, "spec") is None


def test_read_file_refuses_file_and_folder_links(tmp_path: Path) -> None:
    home = _fixture(tmp_path)
    assert artifact_files.read_file(home, "leak.md") is None
    assert artifact_files.read_file(home, "linkdir/inner.md") is None


def test_absent_artifacts_is_none_and_empty_lists_nothing(tmp_path: Path) -> None:
    assert artifact_files.list_files(tmp_path / "no-home") is None
    home = tmp_path / "home"
    home.mkdir()
    assert artifact_files.list_files(home) is None
    assert artifact_files.read_file(home, "x.md") is None
    (home / "artifacts").mkdir()
    assert artifact_files.list_files(home) == []


def test_symlinked_artifacts_root_counts_as_absent(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    (outside / "spec").mkdir(parents=True)
    (outside / "spec" / "q.md").write_text("SECRET\n")
    home = tmp_path / "home"
    home.mkdir()
    (home / "artifacts").symlink_to(outside)
    assert artifact_files.list_files(home) is None
    assert artifact_files.read_file(home, "spec/q.md") is None


def test_non_directory_artifacts_counts_as_absent(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    (home / "artifacts").write_text("not a folder\n")
    assert artifact_files.list_files(home) is None


def test_a_listed_file_swapped_for_a_link_reads_none(tmp_path: Path) -> None:
    home = _fixture(tmp_path)
    assert "top.md" in (artifact_files.list_files(home) or [])
    top = home / "artifacts" / "top.md"
    top.unlink()
    top.symlink_to(tmp_path / "outside" / "secret.md")
    assert artifact_files.read_file(home, "top.md") is None


def test_a_listed_files_folder_swapped_for_a_link_reads_none(tmp_path: Path) -> None:
    home = _fixture(tmp_path)
    assert "spec/q.md" in (artifact_files.list_files(home) or [])
    decoy = tmp_path / "outside" / "decoy"
    decoy.mkdir()
    (decoy / "q.md").write_text("SECRET\n")
    spec = home / "artifacts" / "spec"
    shutil.rmtree(spec)
    spec.symlink_to(decoy)
    assert artifact_files.read_file(home, "spec/q.md") is None


def test_a_listed_file_swapped_for_a_fifo_reads_none_without_blocking(
    tmp_path: Path,
) -> None:
    home = _fixture(tmp_path)
    top = home / "artifacts" / "top.md"
    top.unlink()
    os.mkfifo(top)
    result: list[bytes | None] = []
    t = threading.Thread(
        target=lambda: result.append(artifact_files.read_file(home, "top.md")),
        daemon=True)
    t.start()
    t.join(timeout=10)
    assert not t.is_alive(), "read_file blocked opening a FIFO"
    assert result == [None]
    assert "top.md" not in (artifact_files.list_files(home) or [])


def test_read_file_refuses_traversal_and_malformed_paths(tmp_path: Path) -> None:
    home = _fixture(tmp_path)
    (home / "state.json").write_text("{}\n")
    for rel in ("../state.json", "/etc/passwd", "spec/../top.md", "./top.md",
                "spec//q.md", "spec/", ""):
        assert artifact_files.read_file(home, rel) is None, rel


_SRC = Path(__file__).resolve().parents[1] / "src" / "imp"
_READER = _SRC / "engine" / "artifact_files.py"
_ARTIFACTS_JOIN = re.compile(
    r"""/\s*["']artifacts["']|["']artifacts["']\s*/"""
    r"""|(?:join(?:path)?|Path)\([^)]*["']artifacts["']""")


def test_only_the_reader_joins_a_path_with_artifacts() -> None:
    hits = [
        f"{p.relative_to(_SRC)}:{n}: {line.strip()}"
        for p in sorted(_SRC.rglob("*.py")) if p != _READER
        for n, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1)
        if _ARTIFACTS_JOIN.search(line)
    ]
    assert hits == []


def test_show_artifact_never_reads_a_live_link(tmp_path: Path, tmp_path_factory) -> None:
    from imp.cli.store import _show_artifact
    _init_repo(tmp_path)
    outside = tmp_path_factory.mktemp("outside") / "secret.md"
    outside.write_text("SECRET\n")
    art = project_state_dir(tmp_path) / "quests" / "lnk1" / "artifacts"
    (art / "spec").mkdir(parents=True)
    (art / "spec" / "lnk1.md").write_text("# spec\n")
    (art / "leak.md").symlink_to(outside)
    assert _show_artifact(tmp_path, "lnk1", "spec/lnk1.md") == b"# spec\n"
    assert _show_artifact(tmp_path, "lnk1", "leak.md") is None
