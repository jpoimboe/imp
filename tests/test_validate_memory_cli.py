from pathlib import Path

import pytest

from imp.providers.validate_memory_cli import main


def _write_memory(root: Path, rel: str, content: str = "Prefer tabs over spaces.\n") -> None:
    p = root / "imp" / "memory" / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")


def test_valid_memory_tree_exits_zero(tmp_path: Path) -> None:
    _write_memory(tmp_path, "all.md")
    assert main([str(tmp_path)]) == 0


def test_absent_memory_tree_exits_zero(tmp_path: Path) -> None:
    # No imp/memory/ dir at all -> empty snapshot -> valid no-op.
    assert main([str(tmp_path)]) == 0


def test_invalid_memory_tree_exits_nonzero_with_errors(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    _write_memory(tmp_path, "bogus/all.md")
    rc = main([str(tmp_path)])
    assert rc == 1
    err = capsys.readouterr().err
    assert "bogus" in err
    assert "not a known workflow" in err
