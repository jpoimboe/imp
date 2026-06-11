# contrib/drivers/telegram/tests/test_packaging.py
from __future__ import annotations

import tomllib
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent


def test_console_script_maps_to_main() -> None:
    data = tomllib.loads((_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    # `command: [imp-telegram]` in imp's remote config relies on this entry point.
    assert data["project"]["scripts"]["imp-telegram"] == "imp_telegram:main"


def test_packaging_has_no_local_ruff_override() -> None:
    # The repo-root [tool.ruff] config must govern contrib lint; a local override
    # would silently diverge `make lint`.
    data = tomllib.loads((_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert "ruff" not in data.get("tool", {})


def test_driver_module_has_python_shebang() -> None:
    first = (_ROOT / "imp_telegram.py").read_text(encoding="utf-8").splitlines()[0]
    assert first == "#!/usr/bin/env python3"
