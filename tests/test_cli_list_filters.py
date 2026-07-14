from __future__ import annotations

import argparse

from imp.cli.main import _selected_list_dispositions


def _ns(**kw: bool) -> argparse.Namespace:
    base = {"active": False, "backlog": False, "merged": False,
            "abandoned": False, "shelved": False, "archived": False}
    base.update(kw)
    return argparse.Namespace(**base)


def test_no_flag_selects_nothing() -> None:
    assert _selected_list_dispositions(_ns()) == set()


def test_single_flag_selects_that_disposition() -> None:
    assert _selected_list_dispositions(_ns(abandoned=True)) == {"abandoned"}
    assert _selected_list_dispositions(_ns(shelved=True)) == {"shelved"}
    assert _selected_list_dispositions(_ns(active=True)) == {"active"}
    assert _selected_list_dispositions(_ns(backlog=True)) == {"backlog"}


def test_flags_union_additively() -> None:
    assert _selected_list_dispositions(
        _ns(merged=True, abandoned=True)) == {"merged", "abandoned"}


def test_archived_expands_to_the_three_archive_dispositions() -> None:
    assert _selected_list_dispositions(
        _ns(archived=True)) == {"merged", "abandoned", "shelved"}
    # --archived unioned with a redundant flag stays the same set.
    assert _selected_list_dispositions(
        _ns(archived=True, merged=True)) == {"merged", "abandoned", "shelved"}
