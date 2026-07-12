from __future__ import annotations

from imp.cli import narrative as nv
from imp.cli.render_text import render_tree


def _events() -> list[dict]:
    return [
        {"id": "g", "parent_id": None, "agent_id": "step:x",
         "type": "awaiting_user_input", "payload": {"prompt": ""}},
        {"id": "c", "parent_id": None, "agent_id": "step:x",
         "type": "user_message",
         "payload": {"role": "user",
                     "content": [{"type": "text", "text": "/refactor src/foo.py"}],
                     "human": True}},
        {"id": "s", "parent_id": None, "agent_id": None,
         "type": "skill_load", "payload": {"name": "refactor", "source": "human"}},
        {"id": "b", "parent_id": None, "agent_id": "step:x",
         "type": "user_message",
         "payload": {"role": "user",
                     "content": [{"type": "text", "text": "BODY\n\nsrc/foo.py\n"}],
                     "human": False}},
    ]


def test_skill_sequence_lean_shows_command_and_load_hides_body() -> None:
    lean = "\n".join(
        render_tree(nv.build_tree(_events()), full=False, color=False, width=80))
    assert "/refactor src/foo.py" in lean
    assert "loading skill refactor" in lean
    assert "BODY" not in lean


def test_skill_sequence_full_shows_body_as_injected_message() -> None:
    full = "\n".join(
        render_tree(nv.build_tree(_events()), full=True, color=False, width=80))
    assert "injected message:" in full
    assert "BODY" in full


def test_search_prose_indexes_command_not_body() -> None:
    from imp.cli.render_helpers import conversation_prose
    prose = conversation_prose(_events())
    # AC-6: the command line is searchable; a token only in the body is not.
    assert "/refactor src/foo.py" in prose
    assert "BODY" not in prose
