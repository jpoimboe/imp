from __future__ import annotations

from imp.cli.main import _should_prompt_opening, _normalize_opening_reply, OPENING_QUESTION


def test_prompts_when_interactive_and_unseeded() -> None:
    assert _should_prompt_opening(seed_text=None, interactive=True) is True


def test_no_prompt_when_opening_message_given() -> None:
    assert _should_prompt_opening(seed_text="add a flag", interactive=True) is False


def test_no_prompt_when_non_interactive() -> None:
    assert _should_prompt_opening(seed_text=None, interactive=False) is False


def test_no_prompt_when_seeded_from_spec_file() -> None:
    assert _should_prompt_opening(
        seed_text="Implement this spec\n...", interactive=True) is False


def test_opening_question_is_a_plain_question() -> None:
    # A static, model-free opening prompt — no tool-loading narration.
    assert "?" in OPENING_QUESTION
    assert "load" not in OPENING_QUESTION.lower()
    assert "tool" not in OPENING_QUESTION.lower()


def test_empty_reply_falls_back_to_unseeded() -> None:
    assert _normalize_opening_reply("") is None
    assert _normalize_opening_reply("  ") is None
    assert _normalize_opening_reply("add a flag") == "add a flag"
