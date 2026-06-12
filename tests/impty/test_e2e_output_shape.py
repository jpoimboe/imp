from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from impty import TerminalSession
from tests.impty.e2e_support import can_pty, seed_minimal_repo
from tests.impty.matchers import assert_screen

pytestmark = pytest.mark.skipif(not can_pty(), reason="pty not available")


def test_imp_config_output_shape_over_a_pty(tmp_path: Path) -> None:
    # No provider needed: imp config only reads the effective config.
    seed_minimal_repo(tmp_path, script={"default": []})
    with TerminalSession([sys.executable, "-m", "imp", "config"],
                         cwd=str(tmp_path),
                         env={**os.environ, "IMP_THEME": "ansi"},
                         cols=100, rows=40) as s:
        rc = s.run_to_completion(hard=30.0)
    assert rc == 0
    assert_screen(
        s,
        contains=("tiers:", "settings:", "scripted", "high", "medium", "low"),
    )
