from __future__ import annotations

from imp.cli import narrative as nv


def test_tiers_resolved_row_carries_codex_warning() -> None:
    b = nv.NarrativeBuilder()
    b.add({"id": "e1", "agent_id": None, "type": "tiers_resolved",
           "payload": {
               "tiers": {
                   "high": {"provider": "codex", "model": "gpt-5.5",
                            "differ_tier": None, "differ_from": None,
                            "warning": "codex runs unsandboxed; ..."},
                   "low": {"provider": "anthropic", "model": "haiku",
                           "differ_tier": None, "differ_from": None},
               },
               "steps": {}}})
    node = next(c for c in b.tree.root.children
                if isinstance(c, nv.TiersResolvedNode))
    by_tier = {r["tier"]: r for r in node.rows}
    assert by_tier["high"]["warning"] == "codex runs unsandboxed; ..."
    # non-codex rows must not gain a truthy warning
    assert not by_tier["low"].get("warning")
