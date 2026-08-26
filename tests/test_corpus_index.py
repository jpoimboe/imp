"""The `imp-knowledge` hub must index the corpus by filename, and the routed docs
must link onward — the measured failure mode is an agent that cannot find a doc
because nothing names its path, and falls back to listing `docs/`."""

from __future__ import annotations

import re
from pathlib import Path

from imp.agents.skills import load_internal_skill
from imp.assets import bundled_skills_dir
from tests._corpus import tracked

HUB = "imp-knowledge"
# The three docs `imp/memory/all.md` names by path — injected into every agent
# call, so they are the corpus entry points and must not be dead ends.
ROUTED = (
    "docs/internals/conventions.md",
    "docs/internals/invariants.md",
    "docs/internals/knowledge-map.md",
)


def _hub() -> str:
    return load_internal_skill(bundled_skills_dir(), HUB)


def _corpus_rel() -> list[str]:
    """Corpus docs as `guide/x.md` / `internals/x.md`, the form the index uses."""
    return [str(p).removeprefix("docs/") for p in tracked("docs/guide", "docs/internals")]


def test_hub_indexes_every_corpus_doc_by_name() -> None:
    body = _hub()
    missing = [rel for rel in _corpus_rel() if rel not in body]
    assert not missing, f"corpus docs absent from the {HUB} index: {missing}"


def test_hub_index_names_no_doc_that_does_not_exist() -> None:
    body = _hub()
    live = set(_corpus_rel())
    named = set(re.findall(r"`((?:guide|internals)/[a-z0-9-]+\.md)`", body))
    assert not (named - live), f"{HUB} indexes missing docs: {sorted(named - live)}"


def test_hub_still_points_at_the_injected_corpus_path() -> None:
    assert "{{ docs.path }}" in _hub()


def test_each_indexed_doc_carries_a_one_line_description() -> None:
    """A bare filename list would not tell an agent which file answers its
    question — every entry needs the `— what it answers` half."""
    body = _hub()
    for rel in _corpus_rel():
        line = next((ln for ln in body.splitlines() if f"`{rel}`" in ln), None)
        assert line, rel
        assert "—" in line and len(line.split("—", 1)[1].split()) >= 4, rel


def test_routed_docs_link_onward_into_the_corpus() -> None:
    """Each entry-point doc must reach at least two corpus siblings, so an agent
    that lands on one is not stranded."""
    for rel in ROUTED:
        text = Path(rel).read_text()
        assert "## See also" in text, rel
        sibs = set(re.findall(r"`((?:guide|internals)/[a-z0-9-]+\.md)`", text))
        sibs.discard(rel.removeprefix("docs/"))
        assert len(sibs) >= 2, f"{rel} links onward to only {sorted(sibs)}"


def test_comment_rule_summary_matches_the_authoritative_partial() -> None:
    """`conventions.md` summarizes the comment rule so an agent grepping it there
    gets a hit, but the operative text lives in the shared coding-stance partial.
    Keep the summary's load-bearing clauses verbatim so the two cannot drift."""
    doc = Path("docs/internals/conventions.md").read_text()
    partial = Path("src/imp/assets/partials/coding-stance.md").read_text()
    assert "## Code comments" in doc
    assert "src/imp/assets/partials/coding-stance.md" in doc
    flat_doc = " ".join(doc.split())
    flat_partial = " ".join(partial.split())
    for clause in ("one short line max", "the WHY is non-obvious"):
        assert clause in flat_doc, f"conventions.md dropped: {clause}"
        assert clause in flat_partial, f"coding-stance.md no longer says: {clause}"


def test_index_scopes_internals_to_imp_on_imp() -> None:
    """The hub ships in every project, so its index must not present imp's own
    engine internals as relevant to a quest in someone else's repo. `guide/`
    entries are listed as generally useful; `internals/` sits under a heading
    that tells an agent to skip them unless the project under change is imp."""
    body = _hub()
    generic_at = body.index("Using imp — relevant in any project:")
    imp_only_at = body.index("Working on imp's own source")
    assert generic_at < imp_only_at, "the imp-only group must come second"
    for rel in _corpus_rel():
        at = body.index(f"`{rel}`")
        if rel.startswith("internals/"):
            assert at > imp_only_at, f"{rel} is listed as generally relevant"
        else:
            assert generic_at < at < imp_only_at, f"{rel} is not in the generic group"
