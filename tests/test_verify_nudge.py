from pathlib import Path

import pytest

from imp.cli.main import _verify_command_nudge
from imp.providers import register_builtin_providers, resolve


@pytest.fixture(autouse=True)
def _registry() -> None:
    resolve.reset_providers_for_tests()
    register_builtin_providers()


def _project(tmp_path: Path, body: str) -> Path:
    (tmp_path / "imp").mkdir()
    (tmp_path / "imp" / "project.yaml").write_text(body)
    return tmp_path


def _seq(*answers: str):
    it = iter(answers)
    return lambda _prompt="": next(it)


def test_non_interactive_warns_and_does_not_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    root = _project(tmp_path, "default-workflow: imp\n")
    errs: list[str] = []
    _verify_command_nudge(
        project_root=root, control_root=root, non_interactive=True,
        input_fn=_seq(), output_fn=lambda _m: None, err_fn=errs.append,
        detect_fn=lambda _r: "make check",
    )
    assert any("make check" in e for e in errs)
    assert "verify_command" not in (root / "imp" / "project.yaml").read_text()


def test_interactive_yes_appends_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    root = _project(tmp_path, "default-workflow: imp\n")
    _verify_command_nudge(
        project_root=root, control_root=root, non_interactive=False,
        input_fn=_seq("y"), output_fn=lambda _m: None, err_fn=lambda _m: None,
        detect_fn=lambda _r: "make check",
    )
    assert 'verify_command: "make check"' in (root / "imp" / "project.yaml").read_text()


def test_no_nudge_when_already_configured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    root = _project(tmp_path, "default-workflow: imp\nverify_command: make test\n")
    errs: list[str] = []
    _verify_command_nudge(
        project_root=root, control_root=root, non_interactive=True,
        input_fn=_seq(), output_fn=lambda _m: None, err_fn=errs.append,
        detect_fn=lambda _r: "make check",
    )
    assert errs == []


def test_no_nudge_when_opted_out(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    root = _project(tmp_path, 'default-workflow: imp\nverify_command: ""\n')
    errs: list[str] = []
    _verify_command_nudge(
        project_root=root, control_root=root, non_interactive=True,
        input_fn=_seq(), output_fn=lambda _m: None, err_fn=errs.append,
        detect_fn=lambda _r: "make check",
    )
    assert errs == []


def test_interactive_skip_writes_optout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    root = _project(tmp_path, "default-workflow: imp\n")
    _verify_command_nudge(
        project_root=root, control_root=root, non_interactive=False,
        input_fn=_seq("skip"), output_fn=lambda _m: None, err_fn=lambda _m: None,
        detect_fn=lambda _r: "make check",
    )
    assert 'verify_command: ""' in (root / "imp" / "project.yaml").read_text()


def test_interactive_edit_writes_custom_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    root = _project(tmp_path, "default-workflow: imp\n")
    _verify_command_nudge(
        project_root=root, control_root=root, non_interactive=False,
        input_fn=_seq("edit", "make ci"), output_fn=lambda _m: None, err_fn=lambda _m: None,
        detect_fn=lambda _r: "make check",
    )
    assert 'verify_command: "make ci"' in (root / "imp" / "project.yaml").read_text()


def test_no_nudge_when_nothing_detected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    root = _project(tmp_path, "default-workflow: imp\n")
    errs: list[str] = []
    _verify_command_nudge(
        project_root=root, control_root=root, non_interactive=True,
        input_fn=_seq(), output_fn=lambda _m: None, err_fn=errs.append,
        detect_fn=lambda _r: None,
    )
    assert errs == []
