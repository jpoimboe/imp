from pathlib import Path

from imp.cli.verify_detect import (
    detect_test_command,
    format_test_cmd_fast_yaml,
    resolve_test_cmd_choice,
)


def test_none_when_nothing_detected(tmp_path: Path) -> None:
    assert detect_test_command(tmp_path) is None


def test_makefile_check_target_wins(tmp_path: Path) -> None:
    (tmp_path / "Makefile").write_text("check: lint test\n\t@echo ok\ntest:\n\t@echo t\n")
    assert detect_test_command(tmp_path) == "make check"


def test_makefile_ci_target(tmp_path: Path) -> None:
    (tmp_path / "Makefile").write_text("ci:\n\t@echo ok\n")
    assert detect_test_command(tmp_path) == "make ci"


def test_makefile_chains_present_targets_cheap_first(tmp_path: Path) -> None:
    (tmp_path / "Makefile").write_text("test:\n\t@echo t\nlint:\n\t@echo l\n")
    assert detect_test_command(tmp_path) == "make lint && make test"


def test_makefile_single_test_target(tmp_path: Path) -> None:
    (tmp_path / "Makefile").write_text("test:\n\t@echo t\n")
    assert detect_test_command(tmp_path) == "make test"


def test_package_json_check_script(tmp_path: Path) -> None:
    (tmp_path / "package.json").write_text('{"scripts": {"check": "x", "test": "y"}}')
    assert detect_test_command(tmp_path) == "npm run check"


def test_package_json_test_only(tmp_path: Path) -> None:
    (tmp_path / "package.json").write_text('{"scripts": {"test": "y"}}')
    assert detect_test_command(tmp_path) == "npm test"


def test_package_json_ci_script(tmp_path: Path) -> None:
    (tmp_path / "package.json").write_text('{"scripts": {"ci": "y"}}')
    assert detect_test_command(tmp_path) == "npm run ci"


def test_cargo(tmp_path: Path) -> None:
    (tmp_path / "Cargo.toml").write_text("[package]\nname='x'\n")
    assert detect_test_command(tmp_path) == "cargo clippy && cargo test"


def test_go_mod(tmp_path: Path) -> None:
    (tmp_path / "go.mod").write_text("module x\n")
    assert detect_test_command(tmp_path) == "go vet ./... && go test ./..."


def test_justfile_check_recipe(tmp_path: Path) -> None:
    (tmp_path / "justfile").write_text("check:\n    echo ok\n")
    assert detect_test_command(tmp_path) == "just check"


def test_justfile_test_recipe_fallback(tmp_path: Path) -> None:
    (tmp_path / "justfile").write_text("test:\n    echo t\n")
    assert detect_test_command(tmp_path) == "just test"


def test_makefile_precedence_over_cargo(tmp_path: Path) -> None:
    (tmp_path / "Makefile").write_text("check:\n\t@echo ok\n")
    (tmp_path / "Cargo.toml").write_text("[package]\n")
    assert detect_test_command(tmp_path) == "make check"


def _seq(*answers: str):
    it = iter(answers)
    return lambda _prompt="": next(it)


def test_resolve_yes_uses_suggestion() -> None:
    assert resolve_test_cmd_choice("make check", _seq("y"), lambda _m: None) == "make check"


def test_resolve_blank_defaults_to_suggestion() -> None:
    assert resolve_test_cmd_choice("make check", _seq(""), lambda _m: None) == "make check"


def test_resolve_unrecognized_defaults_to_suggestion() -> None:
    # A non-explicit opt-out defaults to enabling the suggestion (safer than silently opting out).
    assert (
        resolve_test_cmd_choice("make check", _seq("gobbledygook"), lambda _m: None)
        == "make check"
    )


def test_resolve_edit_uses_entered_command() -> None:
    got = resolve_test_cmd_choice("make check", _seq("edit", "make ci"), lambda _m: None)
    assert got == "make ci"


def test_resolve_edit_blank_falls_back_to_suggestion() -> None:
    got = resolve_test_cmd_choice("make check", _seq("edit", ""), lambda _m: None)
    assert got == "make check"


def test_resolve_skip_opts_out() -> None:
    assert resolve_test_cmd_choice("make check", _seq("skip"), lambda _m: None) == ""


def test_resolve_skip_aliases_opt_out() -> None:
    for ans in ("s", "n", "no"):
        assert (
            resolve_test_cmd_choice("make check", _seq(ans), lambda _m: None) == ""
        ), f"alias {ans!r} should opt out"


def test_format_command_line() -> None:
    assert format_test_cmd_fast_yaml("make check") == 'test_cmd_fast: "make check"\n'


def test_format_optout_line() -> None:
    assert format_test_cmd_fast_yaml("") == 'test_cmd_fast: ""\n'
