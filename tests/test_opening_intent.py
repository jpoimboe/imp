import argparse

from imp.cli.main import (
    SeedResolution,
    _normalize_opening_reply,
    _should_prompt_opening,
    resolve_spec_seed,
)


def _ns(**kw):
    base = dict(title=None, opening=None, spec_file=None, message=None)
    base.update(kw)
    return argparse.Namespace(**base)


def test_typed_message_sets_opening_intent_to_the_intent_text() -> None:
    res = resolve_spec_seed(_ns(opening="add a --json flag"))
    assert isinstance(res, SeedResolution)
    assert res.seed_text == "add a --json flag"
    assert res.opening_intent == "add a --json flag"


def test_spec_file_suppresses_opening_intent(tmp_path) -> None:
    f = tmp_path / "draft.md"
    f.write_text("Implement this spec\n...")
    res = resolve_spec_seed(_ns(spec_file=str(f)))
    assert res.seed_text is not None          # the wrapped draft is the seed
    assert res.opening_intent is None         # but no intent turn is rendered


def test_unseeded_suppresses_opening_intent() -> None:
    res = resolve_spec_seed(_ns())
    assert res.seed_text is None
    assert res.opening_intent is None


def test_prompts_when_interactive_and_unseeded() -> None:
    assert _should_prompt_opening(seed_text=None, interactive=True) is True


def test_no_prompt_when_opening_message_given() -> None:
    assert _should_prompt_opening(seed_text="add a flag", interactive=True) is False


def test_no_prompt_when_non_interactive() -> None:
    assert _should_prompt_opening(seed_text=None, interactive=False) is False


def test_no_prompt_when_seeded_from_spec_file() -> None:
    assert _should_prompt_opening(
        seed_text="Implement this spec\n...", interactive=True) is False


def test_opening_question_constant_is_removed() -> None:
    import imp.cli.main as main_mod
    assert not hasattr(main_mod, "OPENING_QUESTION")


def test_empty_reply_falls_back_to_unseeded() -> None:
    assert _normalize_opening_reply("") is None
    assert _normalize_opening_reply("  ") is None
    assert _normalize_opening_reply("add a flag") == "add a flag"
