"""codex attaches occupancy on a bookkeeping row after the turn; the gauge and
the reincarnation line must read it (the agent_message itself carries usage={})."""
from __future__ import annotations

from typing import Any

from imp.cli import narrative as nv
from imp.cli.render_text import render_tree

AID = "step:code/task-lead"


def _walk(node: Any):
    yield node
    for child in getattr(node, "children", ()) or ():
        yield from _walk(child)


def _codex_turn() -> list[dict[str, Any]]:
    # Exactly what the codex runner writes: an empty-usage agent_message, then
    # the patch row carrying the turn's billing sum plus the rollout reading.
    return [
        {"type": "agent_message", "agent_id": AID,
         "payload": {"provider": "codex", "model": "gpt-5.6-sol",
                     "stop_reason": "end_turn", "usage": {},
                     "raw": {"content": [{"type": "text", "text": "done"}]}}},
        {"type": "agent_message_usage_patch", "agent_id": AID,
         "payload": {"usage": {"input_tokens": 82628},
                     "context_tokens": 22389, "context_window": 258400}},
    ]


def _fed() -> nv.NarrativeBuilder:
    b = nv.NarrativeBuilder()
    for e in _codex_turn():
        b.add(e)
    return b


# AC 7(a)
def test_patch_row_drives_the_context_gauge() -> None:
    b = _fed()
    assert b.latest_prompt_tokens == 22389        # the reading, not the 82628 sum
    assert b.latest_context_window == 258400


# AC 7(b)
def test_patch_row_names_the_context_on_the_reincarnation_line() -> None:
    b = _fed()
    b.add({"type": "reincarnating", "agent_id": None,
           "payload": {"reason": "context threshold crossed"}})
    out = "\n".join(render_tree(b.tree, full=False, color=False, width=80))
    assert "reincarnating — context threshold crossed (22k/258k)" in out


# AC 7(c)
def test_patch_row_does_not_move_the_quest_token_totals() -> None:
    b = _fed()
    # the agent_message carried usage={}; the patch's 82628 is billing already
    # counted elsewhere and must not accumulate here (codex `imp log -s` totals
    # are separate work).
    assert b.tree.tokens.input == 0


# AC 7(d)
def test_patch_row_appends_no_node() -> None:
    b = _fed()
    assert not any(isinstance(n, nv.FallbackLine) for n in _walk(b.tree.root))


def test_step_started_resets_context_gauge() -> None:
    b = nv.NarrativeBuilder()
    b.add({"type": "step_started", "agent_id": None,
           "payload": {"step": "code", "kind": "agent", "phase": "work"}})
    for e in _codex_turn():
        b.add(e)
    assert b.latest_prompt_tokens == 22389
    assert b.latest_context_window == 258400
    # Codex leaves context_pct unpopulated; seed it via a claude_code turn so
    # the reset assertion below is falsifiable.
    b.add({"type": "agent_message", "agent_id": AID,
           "payload": {"provider": "claude_code", "model": "claude-opus-4-5",
                       "stop_reason": "end_turn", "usage": {},
                       "context_pct": 37.5, "context_window": 200_000}})
    assert b.latest_context_pct == 37.5              # demonstrably non-None
    b.add({"type": "step_started", "agent_id": None,
           "payload": {"step": "verify", "kind": "agent", "phase": "work"}})
    assert b.latest_prompt_tokens is None
    assert b.latest_context_pct is None
    assert b.latest_context_window is None


def test_an_unregistered_subagents_patch_leaves_the_top_level_gauge_alone() -> None:
    b = _fed()
    b.add({"type": "agent_message_usage_patch", "agent_id": "sub:t1",
           "payload": {"usage": {}, "context_tokens": 199000,
                       "context_window": 258400}})
    assert b.latest_prompt_tokens == 22389        # own turn holds the gauge
    b.add({"type": "reincarnating", "agent_id": None,
           "payload": {"reason": "context threshold crossed"}})
    out = "\n".join(render_tree(b.tree, full=False, color=False, width=80))
    assert "(199k/258k)" in out                   # ...but the crossing turn is named
