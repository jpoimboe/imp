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


def test_reincarnating_uses_lifecycle_color() -> None:
    b = NarrativeBuilder()
    b.add({"type": "reincarnating", "agent_id": None,
           "payload": {"reason": "context threshold crossed"}})
    out = "\n".join(render_tree(b.tree, full=False, color=True, width=80))
    assert pal._ANSI["wordmark"] in out
