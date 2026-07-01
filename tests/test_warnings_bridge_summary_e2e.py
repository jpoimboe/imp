from __future__ import annotations

from imp.cli import narrative as nv


def _warn(eid: str, message: str) -> dict:
    return {"id": eid, "agent_id": "step:p/s", "type": "runner_warning",
            "payload": {"message": message}}


def test_bridged_warnings_from_several_steps_collapse_into_one_summary_line() -> None:
    # The exact message Part B's bridge emits, once per step across two steps.
    msg = ("claude stderr: Warning: Custom betas are only available for API key "
           "users. Ignoring provided betas.")
    tree = nv.build_tree([
        {"id": "c", "agent_id": None, "type": "change_created",
         "payload": {"id": "abc123", "title": "x"}},
        _warn("w1", msg),   # a bridged stderr warning (one per step, flattened here)
        _warn("w2", msg),   # an identical bridged warning from another step
        {"id": "m", "agent_id": None, "type": "change_merged",
         "payload": {"base": "master"}},
    ])
    summaries = [c for c in tree.root.children
                 if isinstance(c, nv.WarningsSummaryNode)]
    assert len(summaries) == 1
    assert summaries[0].total == 2                 # every occurrence counted
    assert summaries[0].lines == [(msg, 2)]        # one deduped line, (×2)
