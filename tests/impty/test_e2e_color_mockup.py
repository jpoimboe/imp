from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from imp.engine.quest import LAYOUT_VERSION
from imp.engine.state import JsonlEventLog
from imp.paths import project_state_dir
from impty import TerminalSession
from tests.impty.e2e_support import (
    E2E_EXIT_POLL_TIMEOUT,
    can_pty,
    clean_env,
    seed_minimal_repo,
)

pytestmark = pytest.mark.skipif(not can_pty(), reason="pty not available")

_MOCKUP = ("Here is the mockup:\n\n```\n"
           "\x1b[1;33m▌ imp\x1b[0m \x1b[36mplanning\x1b[0m\n```\n")


def _seed_quest(root: Path, quest_id: str, prose: str) -> None:
    home = project_state_dir(root) / "quests" / quest_id
    home.mkdir(parents=True, exist_ok=True)
    (home / "quest.json").write_text(json.dumps(
        {"id": quest_id, "title": "Demo", "base_branch": "main",
         "project_root": str(root), "layout_version": LAYOUT_VERSION}))
    log = JsonlEventLog(home / "events.jsonl")
    log.append(agent_id=None, type="step_started",
               payload={"step": "spec", "kind": "agent", "phase": "design",
                        "model": "m", "provider": "anthropic"})
    log.append(agent_id="step:design/spec", type="agent_message",
               payload={"provider": "anthropic", "model": "m",
                        "stop_reason": "end_turn", "usage": {},
                        "raw": {"content": [{"type": "text", "text": prose}]}})


def test_colored_mockup_reaches_a_real_screen(tmp_path: Path) -> None:
    seed_minimal_repo(tmp_path, script={"default": []})
    _seed_quest(tmp_path, "aaa111", _MOCKUP)

    # NO_COLOR disables color on mere presence, and clean_env inherits it: an
    # ambient one would leave every cell unstyled and fail this test for the
    # wrong reason.
    env = clean_env(IMP_THEME="ansi")
    env.pop("NO_COLOR", None)
    # --no-pager is load-bearing: clean_env inherits the developer's PAGER/LESS, and
    # `imp log` pages on a tty, so without it a PAGER=less LESS=R box blocks until
    # run_to_completion times out. Color is unaffected — stdout is still a tty.
    with TerminalSession([sys.executable, "-m", "imp", "log", "aaa111", "--no-pager"],
                         cwd=str(tmp_path), env=env,
                         cols=100, rows=40) as s:
        rc = s.run_to_completion(hard=E2E_EXIT_POLL_TIMEOUT)
    assert rc == 0, "\n".join(s.lines())

    rows = ["".join(s.cell(x, y).data for x in range(100)) for y in range(40)]
    hits = [y for y, row in enumerate(rows) if "▌ imp planning" in row]
    assert len(hits) == 1, "\n".join(rows)
    y = hits[0]
    col = rows[y].index("▌")

    bar = s.cell(col, y)                       # ESC[1;33m painted these cells
    assert (bar.fg, bar.bold) == ("brown", True)
    plan = s.cell(col + 6, y)                  # ▌, sp, i, m, p, sp = 6; "planning" under ESC[36m
    assert (plan.data, plan.fg) == ("p", "cyan")
    assert "^[[" not in "\n".join(rows)         # no caret noise on screen
