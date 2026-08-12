from pathlib import Path

from imp.cli.verify_detect import (
    DetectedTestCommand,
    detect_test_command,
    format_test_cmd_fast_yaml,
)


def test_none_when_nothing_detected(tmp_path: Path) -> None:
    assert detect_test_command(tmp_path) is None


def test_makefile_check_target_wins(tmp_path: Path) -> None:
    (tmp_path / "Makefile").write_text("check: lint test\n\t@echo ok\ntest:\n\t@echo t\n")
    assert detect_test_command(tmp_path) == DetectedTestCommand(
        "make check", "from Makefile")


def test_makefile_ci_target(tmp_path: Path) -> None:
    (tmp_path / "Makefile").write_text("ci:\n\t@echo ok\n")
    assert detect_test_command(tmp_path) == DetectedTestCommand(
        "make ci", "from Makefile")


def test_makefile_chains_present_targets_cheap_first(tmp_path: Path) -> None:
    (tmp_path / "Makefile").write_text("test:\n\t@echo t\nlint:\n\t@echo l\n")
    assert detect_test_command(tmp_path) == DetectedTestCommand(
        "make lint && make test", "from Makefile")


def test_makefile_single_test_target(tmp_path: Path) -> None:
    (tmp_path / "Makefile").write_text("test:\n\t@echo t\n")
    assert detect_test_command(tmp_path) == DetectedTestCommand(
        "make test", "from Makefile")


def test_package_json_check_script(tmp_path: Path) -> None:
    (tmp_path / "package.json").write_text('{"scripts": {"check": "x", "test": "y"}}')
    assert detect_test_command(tmp_path) == DetectedTestCommand(
        "npm run check", "from package.json")


def test_package_json_test_only(tmp_path: Path) -> None:
    (tmp_path / "package.json").write_text('{"scripts": {"test": "y"}}')
    assert detect_test_command(tmp_path) == DetectedTestCommand(
        "npm test", "from package.json")


def test_package_json_ci_script(tmp_path: Path) -> None:
    (tmp_path / "package.json").write_text('{"scripts": {"ci": "y"}}')
    assert detect_test_command(tmp_path) == DetectedTestCommand(
        "npm run ci", "from package.json")


def test_cargo(tmp_path: Path) -> None:
    (tmp_path / "Cargo.toml").write_text("[package]\nname='x'\n")
    assert detect_test_command(tmp_path) == DetectedTestCommand(
        "cargo clippy && cargo test", "from Cargo.toml")


def test_go_mod(tmp_path: Path) -> None:
    (tmp_path / "go.mod").write_text("module x\n")
    assert detect_test_command(tmp_path) == DetectedTestCommand(
        "go vet ./... && go test ./...", "from go.mod")


def test_justfile_check_recipe(tmp_path: Path) -> None:
    (tmp_path / "justfile").write_text("check:\n    echo ok\n")
    assert detect_test_command(tmp_path) == DetectedTestCommand(
        "just check", "from justfile")


def test_justfile_test_recipe_fallback(tmp_path: Path) -> None:
    (tmp_path / "justfile").write_text("test:\n    echo t\n")
    assert detect_test_command(tmp_path) == DetectedTestCommand(
        "just test", "from justfile")


def test_makefile_precedence_over_cargo(tmp_path: Path) -> None:
    (tmp_path / "Makefile").write_text("check:\n\t@echo ok\n")
    (tmp_path / "Cargo.toml").write_text("[package]\n")
    assert detect_test_command(tmp_path) == DetectedTestCommand(
        "make check", "from Makefile")


def test_format_command_line() -> None:
    assert format_test_cmd_fast_yaml("make check") == 'test_cmd_fast: "make check"\n'


def test_format_needs_none_line() -> None:
    assert format_test_cmd_fast_yaml(False) == "test_cmd_fast: false\n"
