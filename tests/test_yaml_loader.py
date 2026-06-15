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


def test_key_loc_returns_each_keys_line() -> None:
    from pathlib import Path

    import yaml

    from imp.engine._yaml import LineLoader, key_loc
    parsed = yaml.load("alpha: 1\nbeta: 2\n", Loader=LineLoader)
    p = Path("x.yaml")
    assert key_loc(p, parsed, "alpha").line == 1
    assert key_loc(p, parsed, "beta").line == 2


def test_key_loc_falls_back_to_mapping_line_for_unknown_key() -> None:
    from pathlib import Path

    import yaml

    from imp.engine._yaml import LineLoader, key_loc

    # Sub-mapping "inner" starts at line 2; fallback must return line 2, not 1.
    parsed = yaml.load("outer:\n  alpha: 1\n", Loader=LineLoader)
    inner = parsed["outer"]
    assert key_loc(Path("x.yaml"), inner, "missing").line == 2


def test_items_skips_key_lines_marker() -> None:
    import yaml

    from imp.engine._yaml import LineLoader, items
    parsed = yaml.load("alpha: 1\n", Loader=LineLoader)
    assert "__key_lines__" in parsed
    assert [k for k, _ in items(parsed)] == ["alpha"]
