from __future__ import annotations

from importlib.resources import files
from pathlib import Path


def _shipped(name: str) -> Path:
    return Path(str(files("imp.assets").joinpath("playbooks", name)))


def test_ux_and_interrupt_shipped_with_description() -> None:
    for name in ("ux.md", "interrupt.md"):
        text = _shipped(name).read_text(encoding="utf-8")
        assert text.startswith("---\n")
        assert "description:" in text.split("---\n", 2)[1]


def test_dead_code_is_an_artifact_playbook_using_args() -> None:
    text = _shipped("dead-code.md").read_text(encoding="utf-8")
    assert "description:" in text.split("---\n", 2)[1]
    assert "{{ args }}" in text
    # An artifact (code) playbook tells the agent NOT to drive any live surface.
    assert "impty" not in text


def test_originals_no_longer_under_tests_playbooks() -> None:
    repo = Path(__file__).resolve().parent.parent
    assert not (repo / "tests" / "playbooks" / "imp-ux-playbook.md").exists()
    assert not (repo / "tests" / "playbooks" / "interrupt-playbook.md").exists()
