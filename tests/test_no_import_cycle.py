from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parents[1] / "src" / "imp"

# The read primitives LocalStore owns. main.py imports them back one-way.
_MOVED_TO_STORE = frozenset({
    "_INFLIGHT", "_Row", "_TAIL_CHUNK", "_backlog_rows", "_events_have_user_message",
    "_first_genuine_human_turn", "_is_backlog_home", "_iso_from_line", "_journal_last_iso",
    "_last_event_iso", "_last_nonempty_line", "_list_quest_homes", "_load_events_for_id",
    "_merged_after", "_parse_jsonl", "_quest_home", "_quest_meta", "_quest_state",
    "_read_last_line", "_resolve_intent", "_row_for_id", "_search_active", "_search_archived",
    "_search_backlog", "_show_artifact", "_status_row", "_status_rows", "_user_message_text",
    "resolve_display_status",
})


def _module_level_names(path: Path) -> set[str]:
    out: set[str] = set()
    for node in ast.parse(path.read_text()).body:
        if isinstance(node, ast.ClassDef | ast.FunctionDef):
            out.add(node.name)
        elif isinstance(node, ast.Assign):
            out.update(t.id for t in node.targets if isinstance(t, ast.Name))
    return out


@pytest.mark.parametrize("module", [
    "imp.engine.agent_run",
    "imp.agents.subagents",
    "imp.api",
    "imp.cli.store",
    "imp.engine.archive.maintenance",
    "imp.remote.daemon.directory",
    "imp.remote.daemon.discover",
])
def test_module_imports_cold(module: str) -> None:
    """A clean interpreter must import these without a circular-import crash.
    Guards the lazy-import of error_policy/schema in drive_invocation: a
    top-level import there would loop agent_run -> error_policy ->
    providers/__init__ -> anthropic -> agents.subagents (partial). The store
    group guards the reverse direction: store.py owns the read primitives, and
    its importers must not need imp.cli.main to reach them."""
    proc = subprocess.run(
        [sys.executable, "-c", f"import {module}"],
        capture_output=True, text=True)
    assert proc.returncode == 0, (
        f"cold `import {module}` failed:\n{proc.stderr}")


def test_store_owns_the_read_primitives() -> None:
    """The filesystem reads live in store.py, so store.py never imports main.py."""
    store = _SRC / "cli" / "store.py"
    assert "imp.cli.main" not in store.read_text()
    assert _module_level_names(store) >= _MOVED_TO_STORE
    assert not _module_level_names(_SRC / "cli" / "main.py") & _MOVED_TO_STORE


def test_no_src_module_imports_a_moved_name_from_main() -> None:
    """Every moved name is reached through imp.cli.store, never re-exported."""
    offenders = []
    for path in sorted(_SRC.rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.ImportFrom) and node.module == "imp.cli.main":
                bad = sorted({a.name for a in node.names} & _MOVED_TO_STORE)
                if bad:
                    offenders.append(f"{path.relative_to(_SRC)}:{node.lineno} {bad}")
    assert offenders == []
