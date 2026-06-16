from __future__ import annotations

from imp.assets import playbooks_dir, shipped_playbook_paths


def test_playbooks_dir_is_a_real_directory() -> None:
    assert playbooks_dir().is_dir()


def test_shipped_playbook_paths_lists_the_shipped_set() -> None:
    paths = shipped_playbook_paths()
    assert {p.stem for p in paths} >= {"ux", "interrupt", "dead-code"}
    assert all(p.suffix == ".md" for p in paths)
    # Sorted for a stable picker order.
    assert [p.name for p in paths] == sorted(p.name for p in paths)
