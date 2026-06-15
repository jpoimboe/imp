from __future__ import annotations

import imp.cli.main as m


def _explain_help() -> str:
    parser = m._build_parser()
    config_parser = None
    for action in parser._actions:
        if hasattr(action, "choices") and action.choices and "config" in action.choices:
            config_parser = action.choices["config"]
            break
    if config_parser is None:
        raise AssertionError("config subparser not found")
    for action in config_parser._actions:
        if "--explain" in action.option_strings:
            return action.help or ""
    raise AssertionError("config --explain argument not found")


def test_explain_help_is_full_sentence_quality_bar() -> None:
    help_text = _explain_help()
    assert help_text[:1].isupper()
    assert help_text.rstrip().endswith(".")
    assert "tier" in help_text.lower()
