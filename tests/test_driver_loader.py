from pathlib import Path

import pytest

from imp.engine.errors import LoadError
from imp.engine.loader import parse_agent_file

_OUTPUT = "output:\n  status: {type: enum, values: [Success]}\n"


def _write(tmp_path: Path, frontmatter_body: str) -> Path:
    p = tmp_path / "a.md"
    p.write_text(f"---\nname: t\n{frontmatter_body}{_OUTPUT}---\nbody\n")
    return p


def test_drivers_parsed_into_frozenset(tmp_path: Path) -> None:
    agent = parse_agent_file(_write(tmp_path, "drivers: [impty]\n"))
    assert agent.drivers == frozenset({"impty"})


def test_drivers_default_empty_when_absent(tmp_path: Path) -> None:
    agent = parse_agent_file(_write(tmp_path, ""))
    assert agent.drivers == frozenset()


def test_drivers_must_be_a_list_of_strings(tmp_path: Path) -> None:
    with pytest.raises(LoadError, match="'drivers' must be a list of strings"):
        parse_agent_file(_write(tmp_path, "drivers: impty\n"))


def test_unknown_driver_rejected(tmp_path: Path) -> None:
    with pytest.raises(LoadError, match="unknown drivers"):
        parse_agent_file(_write(tmp_path, "drivers: [bogus]\n"))


def test_drivers_with_non_string_elements_rejected(tmp_path: Path) -> None:
    with pytest.raises(LoadError, match="'drivers' must be a list of strings"):
        parse_agent_file(_write(tmp_path, "drivers: [1, 2]\n"))


def test_drivers_explicit_empty_list(tmp_path: Path) -> None:
    agent = parse_agent_file(_write(tmp_path, "drivers: []\n"))
    assert agent.drivers == frozenset()
