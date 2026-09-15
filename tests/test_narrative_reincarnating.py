from imp.cli import palette as pal
from imp.cli.narrative import NarrativeBuilder, build_tree
from imp.cli.render_text import render_tree
from imp.engine.reincarnation import handoff_instruction
from tests._event_fixtures import ev


def test_reincarnating_renders_lifecycle_line_with_reason() -> None:
    b = NarrativeBuilder()
    b.add({"type": "reincarnating", "agent_id": None,
           "payload": {"reason": "context threshold crossed"}})
    out = "\n".join(render_tree(b.tree, full=False, color=False, width=80))
    assert "reincarnating — context threshold crossed" in out


def test_reincarnating_renders_bare_when_no_reason() -> None:
    b = NarrativeBuilder()
    b.add({"type": "reincarnating", "agent_id": None, "payload": {}})
    out = "\n".join(render_tree(b.tree, full=False, color=False, width=80))
    assert "reincarnating" in out
    assert "—" not in out


def test_reincarnating_reports_crossing_context_from_subagent_turn() -> None:
    # The live gauge deliberately ignores subagent turns, but the reincarnating
    # line must report the occupancy that actually crossed the limit — which
    # (as in change loko19) was a subagent's. Sourced from the last agent_message.
    b = NarrativeBuilder()
    b.add({"type": "agent_message", "agent_id": "sub:abc",
           "payload": {"provider": "claude_code", "context_tokens": 166941,
                       "context_window": 200000, "context_pct": 83,
                       "usage": {"input_tokens": 3,
                                 "cache_read_input_tokens": 164000}}})
    b.add({"type": "reincarnating", "agent_id": None,
           "payload": {"reason": "context threshold crossed"}})
    out = "\n".join(render_tree(b.tree, full=False, color=False, width=80))
    assert "reincarnating — context threshold crossed" in out
    # the denominator is the handoff limit (80% of this 200k window), not the window
    assert "167k/160k" in out


def test_reincarnating_reports_the_handoff_limit_not_the_window() -> None:
    # 1M-window model: the limit is the 200k ceiling, so the line explains the
    # crossing instead of reading as a fifth of a huge window.
    b = NarrativeBuilder()
    b.add({"type": "agent_message", "agent_id": "step:p/s",
           "payload": {"provider": "claude_code", "model": "claude-opus-5[1m]",
                       "context_tokens": 203_000, "context_window": 1_000_000,
                       "usage": {}}})
    b.add({"type": "reincarnating", "agent_id": None,
           "payload": {"reason": "context threshold crossed"}})
    out = "\n".join(render_tree(b.tree, full=False, color=False, width=80))
    assert "reincarnating — context threshold crossed (203k/200k)" in out


def test_reincarnating_omits_context_when_no_prior_llm_turn() -> None:
    b = NarrativeBuilder()
    b.add({"type": "reincarnating", "agent_id": None,
           "payload": {"reason": "context threshold crossed"}})
    out = "\n".join(render_tree(b.tree, full=False, color=False, width=80))
    assert "reincarnating — context threshold crossed" in out
    assert "/160k" not in out and "(" not in out


def test_reincarnating_omits_context_when_the_turn_reported_no_window() -> None:
    # No window means no resolvable limit, so nothing is recorded — never guessed.
    b = NarrativeBuilder()
    b.add({"type": "agent_message", "agent_id": "step:p/s",
           "payload": {"provider": "anthropic", "context_tokens": 203_000,
                       "usage": {}}})
    b.add({"type": "reincarnating", "agent_id": None,
           "payload": {"reason": "context threshold crossed"}})
    out = "\n".join(render_tree(b.tree, full=False, color=False, width=80))
    assert "reincarnating — context threshold crossed" in out
    assert "203k" not in out and "(" not in out


def test_reincarnating_uses_lifecycle_color() -> None:
    b = NarrativeBuilder()
    b.add({"type": "reincarnating", "agent_id": None,
           "payload": {"reason": "context threshold crossed"}})
    out = "\n".join(render_tree(b.tree, full=False, color=True, width=80))
    assert pal._ANSI["wordmark"] in out


_SENTINEL = "HANDOFF-SENTINEL: where I left off"


def _reincarnation_events(*, handoff: bool) -> list[dict]:
    # The real threshold-crossing shape: ordinary prose, the injected handoff
    # instruction, the note turn, the lifecycle line, then the reborn step. The
    # note turn carries the crossing occupancy, as the real one does — it is the
    # sole source of the reincarnating line's (167k/160k) annotation.
    note: dict = {"provider": "anthropic", "model": "m", "usage": {},
                  "context_tokens": 166941, "context_window": 200000,
                  "raw": {"content": [{"type": "text", "text": _SENTINEL}]}}
    if handoff:
        note["handoff"] = True
    return [
        ev("1", "quest_created", quest_id=1, title="x"),
        ev("2", "step_started", step="impl", kind="agent", phase="build",
           model="m", provider="anthropic"),
        ev("3", "agent_message", agent_id="step:build/impl", usage={},
           raw={"content": [{"type": "text", "text": "ordinary prose"}]}),
        ev("4", "engine_message", agent_id="step:build/impl", subtype="continuation",
           content=[{"type": "text", "text": handoff_instruction()}]),
        ev("5", "agent_message", agent_id="step:build/impl", **note),
        ev("6", "reincarnating", reason="context threshold crossed"),
        ev("7", "step_started", step="impl", kind="agent", phase="build",
           model="m", provider="anthropic", restarted=False),
    ]


def test_flagged_handoff_note_renders_in_neither_lean_nor_full() -> None:
    tree = build_tree(_reincarnation_events(handoff=True))
    lean = "\n".join(render_tree(tree, full=False, color=False, width=80))
    full = "\n".join(render_tree(tree, full=True, color=False, width=80))
    assert _SENTINEL not in lean
    assert _SENTINEL not in full
    # only the note goes: the lifecycle line and the surrounding prose stay.
    assert "reincarnating — context threshold crossed" in lean
    assert "reincarnating — context threshold crossed" in full
    assert "ordinary prose" in lean
    # the dropped turn is still the source of the crossing occupancy: the drop is
    # at node-build time, downstream of the intake that reads it.
    assert "(167k/160k)" in lean


def test_unflagged_note_still_renders_so_the_drop_keys_on_the_flag() -> None:
    tree = build_tree(_reincarnation_events(handoff=False))
    lean = "\n".join(render_tree(tree, full=False, color=False, width=80))
    assert _SENTINEL in lean
