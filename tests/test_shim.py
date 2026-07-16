from __future__ import annotations

import sys
from pathlib import Path

from imp.engine.shim import imp_shim_dir, write_imp_shim


def test_imp_shim_dir_is_under_quest_dir(tmp_path: Path) -> None:
    assert imp_shim_dir(tmp_path) == tmp_path / "imp-shim"


def test_write_imp_shim_creates_executable_console_script(tmp_path: Path) -> None:
    d = write_imp_shim(tmp_path)
    assert d == tmp_path / "imp-shim"
    shim = d / "imp"
    assert shim.exists()
    assert shim.stat().st_mode & 0o777 == 0o755            # exact 0o755 mode
    body = shim.read_text()
    assert body.startswith(f"#!{sys.executable}\n")        # outer-interpreter shebang
    assert "sys.path.insert(0, " in body                   # outer-root at sys.path[0]
    assert "from imp.cli.main import main" in body         # console-script form
    assert "-m imp" not in body  # never the -m form (namespace-shadow guard)
    assert body.index("sys.path.insert") < body.index("from imp.cli.main")  # order guard


def test_write_imp_shim_embeds_outer_src_root(tmp_path: Path) -> None:
    import imp
    outer_root = str(Path(imp.__file__).resolve().parent.parent)
    body = (write_imp_shim(tmp_path) / "imp").read_text()
    assert f"sys.path.insert(0, {outer_root!r})" in body  # outer imp's src dir, not worktree
