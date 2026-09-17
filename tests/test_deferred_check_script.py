"""The deferred-check guard: converge skips the deferred sweep only when there is
nothing to drain. Exit 1 is the sole skip signal — an unreadable path, an absent
IMP_QUEST_DIR, or a list with one open item all exit 0 and dispatch the agent."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from imp.assets import bundled_workflow_dir

SCRIPT = bundled_workflow_dir("default") / "scripts" / "deferred-check.sh"


def _run(quest_dir: str | None) -> subprocess.CompletedProcess[str]:
    env = {"PATH": os.environ["PATH"]}
    if quest_dir is not None:
        env["IMP_QUEST_DIR"] = quest_dir
    return subprocess.run(["bash", str(SCRIPT)], env=env,
                          capture_output=True, text=True)


def _quest_dir(tmp_path: Path, body: str | None) -> str:
    qd = tmp_path / "home"
    qd.mkdir(parents=True, exist_ok=True)
    if body is not None:
        (qd / "deferred.md").write_text(body)
    return str(qd)


def test_absent_deferred_file_skips_the_sweep(tmp_path: Path) -> None:
    # The case that actually fires: no quest step ever wrote a deferred.md.
    assert _run(_quest_dir(tmp_path, None)).returncode == 1


def test_file_without_the_followups_heading_skips_the_sweep(tmp_path: Path) -> None:
    qd = _quest_dir(tmp_path, "# Deferred\n\nNotes, but no followups list.\n")
    assert _run(qd).returncode == 1


def test_heading_with_no_open_items_skips_the_sweep(tmp_path: Path) -> None:
    qd = _quest_dir(
        tmp_path,
        "# Deferred\n\n## Deferred Followups\n\n"
        "- [x] DEFERRED task-lead: already drained [scope: task] — fixed\n",
    )
    assert _run(qd).returncode == 1


def test_one_open_item_dispatches_the_sweep(tmp_path: Path) -> None:
    qd = _quest_dir(
        tmp_path,
        "# Deferred\n\n## Deferred Followups\n\n"
        "- [ ] DEFERRED task-lead: rename the helper [scope: plan]\n",
    )
    assert _run(qd).returncode == 0


def test_open_item_under_a_later_heading_does_not_count(tmp_path: Path) -> None:
    # The scan is section-scoped: a task-list line outside ## Deferred Followups
    # is not sweep work.
    qd = _quest_dir(tmp_path, "## Deferred Followups\n\n## Notes\n\n- [ ] stray\n")
    assert _run(qd).returncode == 1


def test_needs_decision_item_still_dispatches(tmp_path: Path) -> None:
    # The parse stays dumb on purpose: a tagged item is inert to the drain, but the
    # guard does not replicate the agent's tag logic, so a non-empty list dispatches.
    qd = _quest_dir(
        tmp_path,
        "## Deferred Followups\n\n"
        "- [ ] DEFERRED plan-writer: pick a name [scope: quest] "
        "— needs-decision: confirm the wording\n",
    )
    assert _run(qd).returncode == 0


def test_unreadable_deferred_path_fails_closed(tmp_path: Path) -> None:
    # A directory where the file belongs is unreadable-as-a-file for every uid (the
    # suite runs as root, which reads a mode-000 file anyway). Doubt dispatches.
    qd = _quest_dir(tmp_path, None)
    (Path(qd) / "deferred.md").mkdir()
    assert _run(qd).returncode == 0


def test_missing_quest_dir_fails_closed(tmp_path: Path) -> None:
    assert _run(None).returncode == 0
    assert _run("").returncode == 0


def test_case_and_suffix_variant_heading_dispatches_the_sweep(tmp_path: Path) -> None:
    # Guard must not fail open on an agent-written heading variant
    # such as "## Deferred followups (2)".
    qd = _quest_dir(
        tmp_path,
        "## Deferred followups (2)\n\n"
        "- [ ] DEFERRED task-lead: some open item [scope: plan]\n",
    )
    assert _run(qd).returncode == 0
