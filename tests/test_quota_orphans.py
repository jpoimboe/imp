from __future__ import annotations

import ast
import importlib
import re
from pathlib import Path

import pytest

import imp.cli.quota_share as quota_share
import imp.cli.store as store
import imp.cli.store_proxy as store_proxy

_ROOT = Path(__file__).resolve().parent.parent
_SRC = _ROOT / "src"
_OLD_FOOTNOTE = "estimated to ±1 point of the weekly gauge"
_REMOVED = (
    "quota unavailable; this run recorded no quota readings",
    "quota unmeasurable; the weekly window reset during this run",
    "quota unattributable; no tracked usage in this run's window",
    "quota below measurement resolution; the weekly gauge did not move during"
    " this run",
    _OLD_FOOTNOTE,
)


def _strings(path: Path) -> list[str]:
    """Every string constant, with implicitly concatenated literals already
    joined by the parser."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return [n.value for n in ast.walk(tree)
            if isinstance(n, ast.Constant) and isinstance(n.value, str)]


def test_the_per_run_span_machinery_is_gone() -> None:
    """AC 7."""
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("imp.cli.quota_neighbours")
    for name in ("run_span", "Withheld", "weekly_movement", "quota_attribution",
                 "Units", "units_in_span", "credits_by_index", "QuotaFigure",
                 "QuotaWithheld", "QuotaAttribution", "NO_UNITS", "rollup_units",
                 "_Sample"):
        assert not hasattr(quota_share, name), name
    assert not hasattr(store, "NeighbourQuota")
    assert not hasattr(store.LocalStore, "quota_neighbours")
    assert not hasattr(store_proxy.ProxyStore, "quota_neighbours")
    pattern = re.compile(r"\b(run_span|_window_reset|WINDOW_RESET|NO_TRACKED_USAGE)\b")
    hits = [f"{p.relative_to(_ROOT)}:{n}" for p in sorted(_SRC.rglob("*.py"))
            for n, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1)
            if pattern.search(line)]
    assert hits == []


def test_no_removed_legend_or_old_footnote_survives_in_src() -> None:
    """AC 5(e), and the code half of AC 10(d)."""
    hits = [(str(p.relative_to(_ROOT)), text) for p in sorted(_SRC.rglob("*.py"))
            for s in _strings(p) for text in _REMOVED if text in s]
    assert hits == []


def test_the_old_footnote_is_gone_from_the_docs() -> None:
    """AC 10(d), the prose half: every markdown file under docs/ and src/."""
    pages = sorted([*(_ROOT / "docs").rglob("*.md"), *_SRC.rglob("*.md")])
    assert pages
    hits = [str(p.relative_to(_ROOT)) for p in pages
            if _OLD_FOOTNOTE in p.read_text(encoding="utf-8")]
    assert hits == []
