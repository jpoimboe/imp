from __future__ import annotations

from pathlib import Path

from ruamel.yaml.scalarstring import DoubleQuotedScalarString

from imp.cli.config_edit import SetOutcome, UnsetOutcome, apply_set, apply_unset
from imp.providers.config import parse_config_layer
from tests._init_config import write_real_global_config, write_real_init_config


def test_new_key_lands_above_the_trailing_teaching_blocks(tmp_path: Path) -> None:
    before = write_real_init_config(tmp_path)
    old = before.splitlines(keepends=True)
    assert old[10] == 'test_cmd_fast: "make test"\n'   # the last live key

    result = apply_set(before, ("theme",), "dark")

    new = result.text.splitlines(keepends=True)
    assert result.outcome is SetOutcome.NEW
    assert new[11] == "theme: dark\n"
    assert new[:11] == old[:11]        # every other line byte-identical
    assert new[12:] == old[11:]
    assert "# pythonpath: [src]\n" in result.text
    assert ("# Standing prose instructions for agents are markdown under imp/memory/\n"
            in result.text)


def test_updating_a_live_key_keeps_its_quoting_and_every_other_line(
    tmp_path: Path,
) -> None:
    before = write_real_init_config(tmp_path)
    old = before.splitlines(keepends=True)

    result = apply_set(before, ("test_cmd_fast",), "pytest -q")

    new = result.text.splitlines(keepends=True)
    assert result.outcome is SetOutcome.UPDATED
    assert result.previous == "make test"
    assert new[10] == 'test_cmd_fast: "pytest -q"\n'
    assert new[:10] == old[:10]
    assert new[11:] == old[11:]


def test_setting_pythonpath_fills_the_placeholder_not_the_teaching_line(
    tmp_path: Path,
) -> None:
    before = write_real_init_config(tmp_path)

    result = apply_set(before, ("pythonpath",), ["src"])

    assert result.outcome is SetOutcome.UNCOMMENTED
    assert "\npythonpath: [src]\n" in result.text
    assert ("# pythonpath: worktree-relative import roots prepended to the "
            "gate/agent\n") in result.text
    assert "# pythonpath: [src]" not in result.text
    assert len(result.text.splitlines()) == len(before.splitlines())


def test_commented_nested_key_is_not_hoisted_as_top_level(tmp_path: Path) -> None:
    text = "# vars:\n#   theme: something\ndefault-base: main\n"
    result = apply_set(text, ("theme",), "dark")
    assert result.outcome is SetOutcome.NEW
    assert result.text == "# vars:\n#   theme: something\ndefault-base: main\ntheme: dark\n"


def test_commented_out_block_is_left_alone() -> None:
    before = "default-base: main\n\n# vars:\n#   stack: python 3.12\n"

    result = apply_set(before, ("vars", "stack"), "python")

    assert result.outcome is SetOutcome.NEW
    assert "# vars:\n" in result.text          # the block placeholder survives
    assert "vars:\n  stack: python\n" in result.text


def test_real_global_config_keeps_every_other_line(tmp_path: Path) -> None:
    target = tmp_path / "config.yaml"
    before = write_real_global_config(target)
    old = before.splitlines(keepends=True)
    assert old[2] == "theme: dark  # dark | light | ansi\n"

    result = apply_set(before, ("theme",), "light")

    new = result.text.splitlines(keepends=True)
    assert result.outcome is SetOutcome.UPDATED
    assert new[2].startswith("theme: light")
    assert "# dark | light | ansi" in new[2]
    assert new[:2] == old[:2]
    assert new[3:] == old[3:]     # incl. the trailing remote-driver hint block


def test_list_values_stay_on_one_line() -> None:
    result = apply_set("pythonpath: [src]\n", ("pythonpath",), ["src", "tests"])
    assert result.text == "pythonpath: [src, tests]\n"


def test_a_list_replaces_a_block_sequence_with_one_flow_line(tmp_path: Path) -> None:
    before = write_real_global_config(tmp_path / "config.yaml")
    old = before.splitlines(keepends=True)
    assert old[:2] == ["providers:\n", "- anthropic\n"]   # init writes block style

    result = apply_set(before, ("providers",), ["a", "b"])

    new = result.text.splitlines(keepends=True)
    assert new[0] == "providers: [a, b]\n"
    assert new[1:] == old[2:]


def test_inline_comment_text_survives_a_value_change() -> None:
    result = apply_set("theme: dark  # dark | light | ansi\n", ("theme",), "light")
    assert result.text.startswith("theme: light")
    assert "# dark | light | ansi" in result.text


def test_setting_the_same_value_reports_unchanged() -> None:
    text = "theme: dark\n"
    result = apply_set(text, ("theme",), "dark")
    assert result.outcome is SetOutcome.UNCHANGED
    assert result.text == text


def test_a_requote_rewrites_even_when_the_value_compares_equal() -> None:
    result = apply_set("window: 1:30\n", ("window",), DoubleQuotedScalarString("1:30"))
    assert result.outcome is SetOutcome.UPDATED
    assert result.text == 'window: "1:30"\n'


def test_nested_key_with_a_live_parent_goes_into_the_block() -> None:
    text = "tiers:\n  high: {anthropic: opus}\n\n# trailing\n"
    result = apply_set(text, ("tiers", "high", "openai"), "gpt")
    # the mapping gains a key, so it is re-rendered block: a second key sharing
    # one flow line could never be unset again
    assert result.text == (
        "tiers:\n  high:\n    anthropic: opus\n    openai: gpt\n\n# trailing\n")
    assert result.outcome is SetOutcome.NEW


def test_a_second_namespace_entry_nests_under_the_existing_block() -> None:
    text = ""
    for path, value in ((("tiers", "high", "anthropic"), "opus"),
                        (("tiers", "low", "anthropic"), "haiku")):
        text = apply_set(text, path, value).text
    assert text == (
        "tiers:\n  high:\n    anthropic: opus\n  low:\n    anthropic: haiku\n")

    envs = ""
    for path, value in ((("provider_envs", "a", "env"), "A_KEY"),
                        (("provider_envs", "b", "env"), "B_KEY")):
        envs = apply_set(envs, path, value).text
    assert envs == (
        "provider_envs:\n  a:\n    env: A_KEY\n  b:\n    env: B_KEY\n")


def test_empty_document_gets_the_key() -> None:
    assert apply_set("", ("theme",), "dark").text == "theme: dark\n"


def test_unset_comments_the_key_and_keeps_every_other_line(tmp_path: Path) -> None:
    before = write_real_init_config(tmp_path)
    old = before.splitlines(keepends=True)

    result = apply_unset(before, ("test_cmd_fast",))

    new = result.text.splitlines(keepends=True)
    assert result.outcome is UnsetOutcome.COMMENTED
    assert result.previous == "make test"
    assert new[10] == '# test_cmd_fast: "make test"\n'
    assert new[:10] == old[:10]
    assert new[11:] == old[11:]


def test_set_unset_set_does_not_accumulate_stale_lines(tmp_path: Path) -> None:
    before = write_real_init_config(tmp_path)

    once = apply_unset(before, ("test_cmd_fast",)).text
    again = apply_set(once, ("test_cmd_fast",), "make test")
    twice = apply_unset(again.text, ("test_cmd_fast",)).text

    assert again.outcome is SetOutcome.UNCOMMENTED
    assert again.text == before        # set and unset are exact inverses
    assert twice == once
    assert twice.count("test_cmd_fast: ") == 1
    assert len(twice.splitlines()) == len(before.splitlines())


def test_reclaiming_a_placeholder_keeps_its_inline_comment(tmp_path: Path) -> None:
    before = write_real_global_config(tmp_path / "config.yaml")
    commented = apply_unset(before, ("theme",)).text
    assert "# theme: dark  # dark | light | ansi\n" in commented

    result = apply_set(commented, ("theme",), "light")

    assert result.outcome is SetOutcome.UNCOMMENTED
    assert "theme: light  # dark | light | ansi\n" in result.text
    assert len(result.text.splitlines()) == len(before.splitlines())


def test_unset_comments_out_the_parent_chain(tmp_path: Path) -> None:
    text = "providers: [a]\ntiers:\n  high:\n    anthropic: opus\n\n# tail\ntheme: dark\n"

    result = apply_unset(text, ("tiers", "high", "anthropic"))

    assert result.text == (
        "providers: [a]\n# tiers:\n#   high:\n#     anthropic: opus\n"
        "\n# tail\ntheme: dark\n")
    target = tmp_path / "config.yaml"
    target.write_text(result.text)
    parsed = parse_config_layer(target)
    assert parsed is not None and "tiers" not in parsed[0]


def test_unset_covers_the_indented_span_but_releases_trailing_comments() -> None:
    text = "vars:\n  a: 1\n  b: 2\n\n# next block\ntheme: dark\n"
    result = apply_unset(text, ("vars",))
    assert result.text == (
        "# vars:\n#   a: 1\n#   b: 2\n\n# next block\ntheme: dark\n")


def test_unset_of_an_absent_key_is_a_no_op() -> None:
    text = "theme: dark\n"
    result = apply_unset(text, ("editing_mode",))
    assert result.outcome is UnsetOutcome.ABSENT
    assert result.text == text
    assert apply_unset("", ("theme",)).outcome is UnsetOutcome.ABSENT
    scalar_parent = apply_unset("tiers: foo\n", ("tiers", "high", "anthropic"))
    assert scalar_parent.outcome is UnsetOutcome.ABSENT


def test_unset_refuses_inside_a_multi_line_flow_block() -> None:
    for text, path in (("high: {\n  a: 1,\n  b: 2\n}\n", ("high", "b")),
                       ("tiers:\n  high: {\n    anthropic: opus\n  }\n",
                        ("tiers", "high", "anthropic")),
                       ("{theme: dark,\n editing_mode: vi}\n", ("theme",))):
        result = apply_unset(text, path)
        assert result.outcome is UnsetOutcome.FLOW_BLOCKED, text
        assert result.text == text

    # the closing brace inside the span is still safe to comment out
    kept = apply_unset("high: {anthropic: opus\n}\ntheme: dark\n",
                       ("high", "anthropic"))
    assert kept.outcome is UnsetOutcome.COMMENTED
    assert kept.text == "# high: {anthropic: opus\n# }\ntheme: dark\n"


def test_unset_refuses_when_the_key_shares_its_line() -> None:
    nested = apply_unset("tiers:\n  high: {anthropic: opus, openai: gpt}\n",
                         ("tiers", "high", "openai"))
    assert nested.outcome is UnsetOutcome.SHARED_LINE
    assert nested.text == "tiers:\n  high: {anthropic: opus, openai: gpt}\n"

    root = apply_unset("vars: {a: 1, b: 2}\n", ("vars", "a"))
    assert root.outcome is UnsetOutcome.SHARED_LINE

    # the sole key on a flow line takes the whole line with it, parents included
    lone = apply_unset("tiers:\n  high: {anthropic: opus}\n",
                       ("tiers", "high", "anthropic"))
    assert lone.outcome is UnsetOutcome.COMMENTED
    assert lone.text == "# tiers:\n#   high: {anthropic: opus}\n"


def test_set_then_unset_round_trips_a_second_tier_binding() -> None:
    text = apply_set("tiers:\n  high: {anthropic: opus}\n",
                     ("tiers", "high", "openai"), "gpt").text

    result = apply_unset(text, ("tiers", "high", "openai"))

    assert result.outcome is UnsetOutcome.COMMENTED
    assert result.text == (
        "tiers:\n  high:\n    anthropic: opus\n#     openai: gpt\n")


def test_unset_covers_a_block_sequence_and_a_wrapped_flow_sequence(
    tmp_path: Path,
) -> None:
    before = write_real_global_config(tmp_path / "config.yaml")

    result = apply_unset(before, ("providers",))

    assert result.text.startswith("# providers:\n# - anthropic\ntheme: dark")
    assert result.text.splitlines()[2:] == before.splitlines()[2:]

    wrapped = apply_unset("theme: dark\npythonpath: [\n  src,\n  tests,\n]\n",
                          ("pythonpath",))
    assert wrapped.text == (
        "theme: dark\n# pythonpath: [\n#   src,\n#   tests,\n# ]\n")


def test_unset_covers_a_block_scalar() -> None:
    result = apply_unset("test_cmd_fast: |\n  a\n  b\ntheme: dark\n",
                         ("test_cmd_fast",))
    assert result.text == "# test_cmd_fast: |\n#   a\n#   b\ntheme: dark\n"
