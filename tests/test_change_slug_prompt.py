from imp.agents.prompts import build_context, render_prompt


def test_change_handle_and_title_render() -> None:
    ctx = build_context(change_handle="add-oauth", change_title="Add OAuth",
                        event_log=_EmptyLog())
    out = render_prompt("handle={{ change.handle }} title={{ change.title }}", ctx)
    assert out == "handle=add-oauth title=Add OAuth"


class _EmptyLog:
    def read(self):
        return iter(())
