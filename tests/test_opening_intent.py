import argparse

from imp.cli.main import (
    SeedResolution,
    _normalize_opening_reply,
    _should_prompt_opening,
    resolve_spec_seed,
)


def _ns(**kw):
    base = dict(title=None, opening=None, message=None)
    base.update(kw)
    return argparse.Namespace(**base)


def test_typed_message_sets_seed_text_to_the_intent_text() -> None:
    res = resolve_spec_seed(_ns(opening="add a --json flag"))
    assert isinstance(res, SeedResolution)
    assert res.seed_text == "add a --json flag"


def test_unseeded_suppresses_seed_text() -> None:
    res = resolve_spec_seed(_ns())
    assert res.seed_text is None


def test_prompts_when_interactive_and_unseeded() -> None:
    assert _should_prompt_opening(seed_text=None, interactive=True) is True


def test_no_prompt_when_opening_message_given() -> None:
    assert _should_prompt_opening(seed_text="add a flag", interactive=True) is False


def test_no_prompt_when_non_interactive() -> None:
    assert _should_prompt_opening(seed_text=None, interactive=False) is False


def test_opening_question_helpers_are_removed() -> None:
    import imp.cli.main as main_mod
    assert not hasattr(main_mod, "OPENING_QUESTION_NEW")
    assert not hasattr(main_mod, "OPENING_QUESTION_CHAT")
    assert not hasattr(main_mod, "_opening_question")


def test_opening_hint_is_kept() -> None:
    import imp.cli.main as main_mod
    assert main_mod.OPENING_HINT == "type a message, /help for commands, or /model to switch models"


def test_empty_reply_falls_back_to_unseeded() -> None:
    assert _normalize_opening_reply("") is None
    assert _normalize_opening_reply("  ") is None
    assert _normalize_opening_reply("add a flag") == "add a flag"
