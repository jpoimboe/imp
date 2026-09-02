from __future__ import annotations

import pytest

from imp.cli import splash
from scripts import capture_imp_ui


def test_scene_splash_all_scenes_labels_and_renders_the_complete_pool(capsys) -> None:
    capture_imp_ui.scene_splash(
        color=False, width=80, theme="dark", all_scenes=True)

    out = capsys.readouterr().out
    labels = [line for line in out.splitlines() if line.startswith("[")]
    assert labels == [
        f"[{index:02d}] {builder.__name__}"
        for index, (builder, _phrase) in enumerate(splash.SCENES)
    ]
    for _builder, phrase in splash.SCENES:
        assert phrase in out


def test_scene_splash_single_scene_keeps_unlabelled_output(capsys) -> None:
    capture_imp_ui.scene_splash(
        color=False, width=80, theme="dark", index=1)

    out = capsys.readouterr().out
    assert "[01]" not in out
    assert out.endswith(splash.SCENES[1][1] + "\n")


def test_all_scenes_rejects_non_splash_modes() -> None:
    with pytest.raises(SystemExit, match="2"):
        capture_imp_ui.main(["stream", "--all-scenes"])
