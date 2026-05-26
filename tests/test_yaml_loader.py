"""LineLoader YAML Norway-problem regression tests (Bug F).

test_loader.py exceeds 500 lines; Norway-problem coverage lives here.
"""

from __future__ import annotations

# --- Bug F: YAML 1.1 Norway problem ---


def test_yes_no_on_off_parse_as_strings() -> None:
    """YAML 1.1's Norway problem: Yes/No/On/Off normally parse as booleans.
    Our LineLoader strips that so workflow labels don't get coerced."""
    import yaml

    from imp.engine._yaml import LineLoader
    for word in ("Yes", "No", "On", "Off", "yes", "no", "on", "off",
                 "YES", "NO", "ON", "OFF"):
        parsed = yaml.load(f"val: {word}", Loader=LineLoader)
        assert parsed["val"] == word, (
            f"{word!r} parsed as {parsed['val']!r}, "
            f"expected the literal string"
        )


def test_true_false_still_parse_as_booleans() -> None:
    """True/False/true/false keep their boolean semantics (only Yes/No/On/Off
    are de-tagged)."""
    import yaml

    from imp.engine._yaml import LineLoader
    for word, expected in (("true", True), ("True", True), ("TRUE", True),
                            ("false", False), ("False", False), ("FALSE", False)):
        parsed = yaml.load(f"val: {word}", Loader=LineLoader)
        assert parsed["val"] is expected, (
            f"{word!r} parsed as {parsed['val']!r} (type {type(parsed['val']).__name__})"
        )
