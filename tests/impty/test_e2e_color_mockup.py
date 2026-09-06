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

# luge78's message #596, byte-for-byte as the model typed it: no ESC anywhere in this
# file. Seeding one from Python is what let this test pass over an unreachable feature.
_MOCKUP = ("Here is the mockup:\n\n```\n"
           "[38;2;255;154;60mwarning: [auth] retry 2 (1.0s) — another session "
           "rotated the OAuth token; retrying with the new one[0m\n```\n")


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
    # 99 content columns + the 2-col turn hang: 120 keeps the mockup on one row, so
    # the colored cells sit at a known column.
    with TerminalSession([sys.executable, "-m", "imp", "log", "aaa111", "--no-pager"],
                         cwd=str(tmp_path), env=env,
                         cols=120, rows=40) as s:
        rc = s.run_to_completion(hard=E2E_EXIT_POLL_TIMEOUT)
    assert rc == 0, "\n".join(s.lines())

    rows = ["".join(s.cell(x, y).data for x in range(120)) for y in range(40)]
    hits = [y for y, row in enumerate(rows) if "warning: [auth] retry 2 (1.0s)" in row]
    assert len(hits) == 1, "\n".join(rows)
    y = hits[0]
    col = rows[y].index("warning")

    warn = s.cell(col, y)                       # [38;2;255;154;60m painted these cells
    assert (warn.data, warn.fg) == ("w", "ff9a3c")   # pyte stores truecolor as hex
    screen = "\n".join(rows)
    assert "[38;2" not in screen                # the code itself never reaches a cell
    assert "^[[" not in screen                  # no caret noise on screen
