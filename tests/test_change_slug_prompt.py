from imp.agents.prompts import build_context, render_prompt


def test_change_id_and_title_render() -> None:
    ctx = build_context(change_title="Add OAuth", event_log=_EmptyLog(),
                        change_id="abc123")
    out = render_prompt("id={{ change.id }} title={{ change.title }}", ctx)
    assert out == "id=abc123 title=Add OAuth"


class _EmptyLog:
    def read(self):
        return iter(())
