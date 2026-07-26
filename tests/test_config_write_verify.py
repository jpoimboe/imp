from __future__ import annotations

import stat
from pathlib import Path

import pytest

from imp.cli.config_write import land, read_back, reads_back_as, scratch_seam, verify
from imp.engine.errors import LoadError

_LAYER = (
    "provider_envs:\n  openai: {env: OPENAI_KEY}\n"
    "remote:\n  driver: telegram\n"
    "providers: [a, b]\n"
    "facts:\n  window: 1:30\n"
    "default-base: main\n"
)


def test_read_back_uses_imps_own_readers(tmp_path: Path) -> None:
    layer = tmp_path / "config.yaml"
    layer.write_text(_LAYER)

    assert read_back(layer, ("provider_envs", "openai")) == "OPENAI_KEY"
    assert read_back(layer, ("remote", "driver", "name")) == "telegram"
    assert list(read_back(layer, ("providers",))) == ["a", "b"]
    assert read_back(layer, ("default-base",)) == "main"
    assert read_back(layer, ("theme",)) is None
    # facts go through LineLoader (YAML 1.1), where 1:30 is sexagesimal 90
    assert read_back(layer, ("facts", "window")) == "90"


def test_read_back_preserves_var_override_types(tmp_path: Path) -> None:
    layer = tmp_path / "config.yaml"
    layer.write_text("vars:\n  flag: true\n  count: 5\n  name: hello\n")

    assert read_back(layer, ("vars", "flag")) is True
    assert read_back(layer, ("vars", "count")) == 5
    assert read_back(layer, ("vars", "name")) == "hello"
    assert not reads_back_as(read_back(layer, ("vars", "count")), True)


def test_reads_back_as_is_type_exact() -> None:
    assert reads_back_as(("a", "b"), ["a", "b"])
    assert reads_back_as(240, 240)
    assert reads_back_as(True, True)
    assert not reads_back_as(1, True)
    assert not reads_back_as("90", "1:30")
    assert not reads_back_as(None, "x")


def test_verify_raises_the_validators_own_message_without_the_scratch_path(
    tmp_path: Path,
) -> None:
    with (scratch_seam(tmp_path / ".imp" / "config-verify-1", "config.yaml") as scratch,
          pytest.raises(LoadError) as excinfo):
        verify(scratch, "theme: purple\n", ("theme",), "purple")
    assert excinfo.value.message == "'theme' must be 'dark', 'light', or 'ansi'"
    assert "config-verify" not in excinfo.value.message


def test_verify_reports_a_value_that_does_not_read_back(tmp_path: Path) -> None:
    with scratch_seam(tmp_path / "scratch", "config.yaml") as scratch:
        assert verify(scratch, "facts:\n  window: 1:30\n",
                      ("facts", "window"), "1:30") is False
        assert verify(scratch, 'facts:\n  window: "1:30"\n',
                      ("facts", "window"), "1:30") is True


def test_scratch_seam_uses_the_targets_basename_and_always_cleans_up(
    tmp_path: Path,
) -> None:
    scratch_dir = tmp_path / ".imp" / "config-verify-7"
    with scratch_seam(scratch_dir, "config.yaml") as scratch:
        assert scratch.name == "config.yaml"
        assert scratch.parent == scratch_dir
        scratch.write_text("theme: dark\n")
    assert not scratch_dir.exists()

    with pytest.raises(LoadError), scratch_seam(scratch_dir, "config.yaml") as scratch:
        verify(scratch, "theme: purple\n", ("theme",), "purple")
    assert not scratch_dir.exists()


def test_land_moves_the_verified_bytes_onto_the_target(tmp_path: Path) -> None:
    target = tmp_path / "config.yaml"
    target.write_text("theme: dark\n")
    with scratch_seam(tmp_path / "scratch", target.name) as scratch:
        assert verify(scratch, "theme: light\n", ("theme",), "light")
        land(scratch, target)
    assert target.read_text() == "theme: light\n"


def test_land_keeps_the_targets_file_mode(tmp_path: Path) -> None:
    target = tmp_path / "config.yaml"
    target.write_text("theme: dark\n")
    target.chmod(0o600)

    with scratch_seam(tmp_path / "scratch", target.name) as scratch:
        assert verify(scratch, "theme: light\n", ("theme",), "light")
        scratch.chmod(0o644)
        assert stat.S_IMODE(scratch.stat().st_mode) != 0o600
        land(scratch, target)

    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert target.read_text() == "theme: light\n"
