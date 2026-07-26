from __future__ import annotations

from pathlib import Path

from ruamel.yaml.scalarstring import DoubleQuotedScalarString

from imp.cli.config_edit import SetOutcome, apply_set
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
    assert "# facts:\n" in result.text


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


def test_commented_out_block_is_left_alone(tmp_path: Path) -> None:
    before = write_real_init_config(tmp_path)

    result = apply_set(before, ("facts", "stack"), "python")

    assert result.outcome is SetOutcome.NEW
    assert "# facts:\n" in result.text          # the block placeholder survives
    assert "facts:\n  stack: python\n" in result.text


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
