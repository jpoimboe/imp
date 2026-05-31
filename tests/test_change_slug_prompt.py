from imp.agents.prompts import build_context, render_prompt


def test_change_slug_and_title_render() -> None:
    ctx = build_context(change_slug="add-oauth", change_title="Add OAuth",
                        event_log=_EmptyLog())
    out = render_prompt("slug={{ change.slug }} title={{ change.title }}", ctx)
    assert out == "slug=add-oauth title=Add OAuth"


class _EmptyLog:
    def read(self):
        return iter(())
