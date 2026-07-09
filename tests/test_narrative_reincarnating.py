from imp.cli import palette as pal
from imp.cli.narrative import NarrativeBuilder
from imp.cli.render_text import render_tree


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
    # line must report the occupancy that actually crossed the checkpoint — which
    # (as in change loko19) was a subagent's. Sourced from the last llm_response.
    b = NarrativeBuilder()
    b.add({"type": "llm_response", "agent_id": "sub:abc",
           "payload": {"provider": "claude_code", "context_tokens": 166941,
                       "context_window": 200000, "context_pct": 83,
                       "usage": {"input_tokens": 3,
                                 "cache_read_input_tokens": 164000}}})
    b.add({"type": "reincarnating", "agent_id": None,
           "payload": {"reason": "context threshold crossed"}})
    out = "\n".join(render_tree(b.tree, full=False, color=False, width=80))
    assert "reincarnating — context threshold crossed" in out
    # window shown (dispels "was it a 1M window?"), occupancy shown as tokens.
    assert "167k/200k" in out


def test_reincarnating_omits_context_when_no_prior_llm_turn() -> None:
    b = NarrativeBuilder()
    b.add({"type": "reincarnating", "agent_id": None,
           "payload": {"reason": "context threshold crossed"}})
    out = "\n".join(render_tree(b.tree, full=False, color=False, width=80))
    assert "reincarnating — context threshold crossed" in out
    assert "/200k" not in out and "(" not in out


def test_reincarnating_uses_lifecycle_color() -> None:
    b = NarrativeBuilder()
    b.add({"type": "reincarnating", "agent_id": None,
           "payload": {"reason": "context threshold crossed"}})
    out = "\n".join(render_tree(b.tree, full=False, color=True, width=80))
    assert pal._ANSI["wordmark"] in out
