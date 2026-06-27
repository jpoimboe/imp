from __future__ import annotations

from pathlib import Path

from imp.agents.tools.symbol_outline import detect_language


def test_detect_language_by_extension() -> None:
    assert detect_language(Path("pkg/foo.py")) == "python"
    assert detect_language(Path("a/b/foo.ts")) == "typescript"
    assert detect_language(Path("svc/main.go")) == "go"
    assert detect_language(Path("src/lib.rs")) == "rust"


def test_detect_language_filename_fallback() -> None:
    assert detect_language(Path("Makefile")) == "make"
    assert detect_language(Path("build/Dockerfile")) == "dockerfile"


def test_detect_language_unknown_returns_none() -> None:
    assert detect_language(Path("notes.unknownext")) is None
    assert detect_language(Path("LICENSE")) is None
