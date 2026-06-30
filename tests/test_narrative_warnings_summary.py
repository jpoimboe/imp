from imp.cli import narrative as nv


def _warn(eid: str, message: str) -> dict:
    return {"id": eid, "agent_id": "step:p/s", "type": "runner_warning",
            "payload": {"message": message}}


def _merged(eid: str = "m") -> dict:
    return {"id": eid, "agent_id": None, "type": "change_merged",
            "payload": {"base": "master"}}


def _summary_nodes(root) -> list[nv.WarningsSummaryNode]:
    return [c for c in root.children if isinstance(c, nv.WarningsSummaryNode)]


def test_merge_terminal_flushes_one_summary_with_total_dedup_and_order() -> None:
    tree = nv.build_tree([
        {"id": "c", "agent_id": None, "type": "change_created",
         "payload": {"id": "abc123", "title": "x"}},
        _warn("w1", "alpha"),
        _warn("w2", "beta"),
        _warn("w3", "alpha"),
        _merged(),
    ])
    summaries = _summary_nodes(tree.root)
    assert len(summaries) == 1
    s = summaries[0]
    # total = every occurrence (3 = alpha×2 + beta×1), not the distinct-line count.
    assert s.total == 3
    # deduped, first-seen order, with per-line counts.
    assert s.lines == [("alpha", 2), ("beta", 1)]


def test_summary_sits_immediately_before_merged_node_at_root() -> None:
    tree = nv.build_tree([
        {"id": "c", "agent_id": None, "type": "change_created",
         "payload": {"id": "abc123", "title": "x"}},
        _warn("w1", "alpha"),
        _merged(),
    ])
    kinds = [type(c).__name__ for c in tree.root.children]
    si = kinds.index("WarningsSummaryNode")
    mi = kinds.index("MergedNode")
    assert si + 1 == mi  # adjacent, summary directly above the outcome


def test_zero_warnings_produces_no_summary_node() -> None:
    tree = nv.build_tree([
        {"id": "c", "agent_id": None, "type": "change_created",
         "payload": {"id": "abc123", "title": "x"}},
        _merged(),
    ])
    assert _summary_nodes(tree.root) == []


def test_flush_helper_is_idempotent() -> None:
    b = nv.NarrativeBuilder()
    b.add(_warn("w1", "alpha"))
    b._flush_warnings_summary()
    b._flush_warnings_summary()
    assert len(_summary_nodes(b.tree.root)) == 1


def _started(eid: str, phase: str = "design", step: str = "spec") -> dict:
    return {"id": eid, "agent_id": None, "type": "step_started",
            "payload": {"step": step, "kind": "agent", "phase": phase}}


def test_summary_lands_at_root_even_with_open_phase() -> None:
    # A phase/step is still open when merge fires; the summary must bypass _append
    # and land at the workflow root, not nested inside the open phase.
    tree = nv.build_tree([
        {"id": "c", "agent_id": None, "type": "change_created",
         "payload": {"id": "abc123", "title": "x"}},
        _started("s1"),
        _warn("w1", "alpha"),
        _merged(),
    ])
    summaries = [c for c in tree.root.children
                 if isinstance(c, nv.WarningsSummaryNode)]
    assert len(summaries) == 1  # at root, not nested in the open phase


def test_inline_runner_warning_node_still_appended() -> None:
    # The existing inline ErrorNode(is_warning=True) render is unchanged.
    tree = nv.build_tree([_warn("w1", "alpha")])
    errs = [c for c in tree.root.children
            if isinstance(c, nv.ErrorNode) and c.is_warning]
    assert len(errs) == 1
    assert errs[0].message == "alpha"
