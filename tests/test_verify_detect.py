from pathlib import Path

from imp.cli.verify_detect import detect_verify_command


def test_none_when_nothing_detected(tmp_path: Path) -> None:
    assert detect_verify_command(tmp_path) is None


def test_makefile_check_target_wins(tmp_path: Path) -> None:
    (tmp_path / "Makefile").write_text("check: lint test\n\t@echo ok\ntest:\n\t@echo t\n")
    assert detect_verify_command(tmp_path) == "make check"


def test_makefile_ci_target(tmp_path: Path) -> None:
    (tmp_path / "Makefile").write_text("ci:\n\t@echo ok\n")
    assert detect_verify_command(tmp_path) == "make ci"


def test_makefile_chains_present_targets_cheap_first(tmp_path: Path) -> None:
    (tmp_path / "Makefile").write_text("test:\n\t@echo t\nlint:\n\t@echo l\n")
    assert detect_verify_command(tmp_path) == "make lint && make test"


def test_makefile_single_test_target(tmp_path: Path) -> None:
    (tmp_path / "Makefile").write_text("test:\n\t@echo t\n")
    assert detect_verify_command(tmp_path) == "make test"


def test_package_json_check_script(tmp_path: Path) -> None:
    (tmp_path / "package.json").write_text('{"scripts": {"check": "x", "test": "y"}}')
    assert detect_verify_command(tmp_path) == "npm run check"


def test_package_json_test_only(tmp_path: Path) -> None:
    (tmp_path / "package.json").write_text('{"scripts": {"test": "y"}}')
    assert detect_verify_command(tmp_path) == "npm test"


def test_package_json_ci_script(tmp_path: Path) -> None:
    (tmp_path / "package.json").write_text('{"scripts": {"ci": "y"}}')
    assert detect_verify_command(tmp_path) == "npm run ci"


def test_cargo(tmp_path: Path) -> None:
    (tmp_path / "Cargo.toml").write_text("[package]\nname='x'\n")
    assert detect_verify_command(tmp_path) == "cargo clippy && cargo test"


def test_go_mod(tmp_path: Path) -> None:
    (tmp_path / "go.mod").write_text("module x\n")
    assert detect_verify_command(tmp_path) == "go vet ./... && go test ./..."


def test_justfile_check_recipe(tmp_path: Path) -> None:
    (tmp_path / "justfile").write_text("check:\n    echo ok\n")
    assert detect_verify_command(tmp_path) == "just check"


def test_justfile_test_recipe_fallback(tmp_path: Path) -> None:
    (tmp_path / "justfile").write_text("test:\n    echo t\n")
    assert detect_verify_command(tmp_path) == "just test"


def test_makefile_precedence_over_cargo(tmp_path: Path) -> None:
    (tmp_path / "Makefile").write_text("check:\n\t@echo ok\n")
    (tmp_path / "Cargo.toml").write_text("[package]\n")
    assert detect_verify_command(tmp_path) == "make check"
