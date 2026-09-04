# SPDX-License-Identifier: MIT
# Copyright 2026 Josh Poimboeuf
"""`imp config set` / `imp config unset`: pick the layer, edit the file, and land
only bytes imp's own readers accept."""

from __future__ import annotations

import json
import os
import shutil
import stat
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from pathlib import Path
from typing import Any

from ruamel.yaml.error import YAMLError
from ruamel.yaml.scalarstring import DoubleQuotedScalarString

from imp.cli.config_cmd import layer_label
from imp.cli.config_edit import (
    SetOutcome,
    SetResult,
    UnsetOutcome,
    apply_set,
    apply_unset,
)
from imp.cli.config_keys import Layer, SettableKey, resolve_key, unknown_key_message
from imp.engine._yaml import config_yaml_error
from imp.engine.errors import LoadError
from imp.paths import project_state_dir
from imp.providers.config import (
    parse_config_layer,
    parse_default_base,
)
from imp.providers.effective import project_rename_hint
from imp.providers.paths import global_config_path


def read_back(file: Path, read_path: tuple[str, ...]) -> Any:
    """The value imp's own readers see at `read_path`, or None when absent."""
    head = read_path[0]
    if head == "default-base":
        return parse_default_base(file)
    parsed = parse_config_layer(file)
    node: Any = {} if parsed is None else parsed[0]
    for seg in read_path:
        if not isinstance(node, dict) or seg not in node:
            return None
        node = node[seg]
    return node


def reads_back_as(got: Any, expected: Any) -> bool:
    if isinstance(expected, list):
        return isinstance(got, (list, tuple)) and list(got) == expected
    return type(got) is type(expected) and got == expected


@contextmanager
def scratch_seam(directory: Path, name: str) -> Iterator[Path]:
    """A real file under the target's own basename, so validator messages already
    name `config.yaml` and `os.replace` lands from the same filesystem. Any
    directory this had to create goes away again unless a write landed in it."""
    created: list[Path] = []
    for path in (directory, *directory.parents):
        if path.exists():
            break
        created.append(path)
    try:
        directory.mkdir(parents=True, exist_ok=True)
        yield directory / name
    finally:
        shutil.rmtree(directory, ignore_errors=True)
        for path in created:
            with suppress(OSError):
                path.rmdir()


def verify_readable(scratch_file: Path, text: str) -> None:
    """Raise LoadError unless imp's layer reader accepts the whole candidate —
    every key, not only the one being written."""
    scratch_file.write_text(text)
    parse_config_layer(scratch_file)


def verify(scratch_file: Path, text: str, read_path: tuple[str, ...],
           expected: Any) -> bool:
    verify_readable(scratch_file, text)
    return reads_back_as(read_back(scratch_file, read_path), expected)


def land(scratch_file: Path, target: Path) -> None:
    # The scratch file is born at the process umask; a target the user tightened
    # must not come back 644.
    if target.exists():
        os.chmod(scratch_file, stat.S_IMODE(target.stat().st_mode))
    os.replace(scratch_file, target)


class ConfigWriteError(Exception):
    """A message for the `imp:` line; the target file is never touched."""


def _emit_line(line: str) -> None:
    """Write confirmations stay byte-clean — no trailing blank line on a tty
    (docs/internals/conventions.md)."""
    print(line)


def _emit_error(message: str) -> None:
    # Deferred: imp.cli.main imports this module.
    from imp.cli.main import _emit_cli_error
    _emit_cli_error(message)


def _plain(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(str(item) for item in value) + "]"
    return str(value)


def _render(value: Any) -> str:
    return json.dumps(value) if isinstance(value, str) else _plain(value)


def _suffix(result: SetResult) -> str:
    if result.outcome is SetOutcome.UPDATED:
        return f"(was {_render(result.previous)})"
    if result.outcome is SetOutcome.UNCOMMENTED:
        return "(was commented out)"
    if result.outcome is SetOutcome.UNCHANGED:
        return "(unchanged)"
    return "(new)"


def _settable(key: str) -> SettableKey:
    resolved = resolve_key(key)
    if resolved is None:
        raise ConfigWriteError(unknown_key_message(key))
    return resolved


def _target(key: SettableKey, scope: Layer | None,
            control_root: Path) -> tuple[Layer, Path]:
    layer: Layer = scope or key.home
    if layer in ("global", "local"):
        if key.project_only:
            raise ConfigWriteError(
                f"{key.dotted!r} is a project setting; drop --{layer}")
        if layer == "global":
            return "global", global_config_path()
        return "local", project_state_dir(control_root) / "config.yaml"
    return "project", control_root / "imp" / "config.yaml"


def _require_project_file(target: Path, control_root: Path) -> None:
    if target.is_file():
        return
    if (control_root / "imp" / "project.yaml").is_file():
        raise ConfigWriteError(project_rename_hint(control_root))
    raise ConfigWriteError(
        f"no imp/config.yaml in {control_root} — run 'imp init' first")


def scratch_dir(target: Path) -> Path:
    return target.parent / f".config-verify-{os.getpid()}"


def refusal(target: Path, dotted: str) -> str:
    return (f"refusing to write {layer_label(target)} — "
            f"{dotted!r} would not read back as given")


def _emit_shadow(key: SettableKey, layer: Layer, control_root: Path) -> None:
    if key.project_only or layer == "local":
        return
    above = [global_config_path()] if layer == "project" else []
    if (control_root / "imp" / "config.yaml").is_file():
        above.append(project_state_dir(control_root) / "config.yaml")
    winner: Path | None = None
    effective: Any = None
    for src in above:
        if not src.is_file():
            continue
        try:
            value = read_back(src, key.read_path)
        except LoadError:
            continue   # a broken higher layer must not fail a landed write
        if value is not None:
            winner, effective = src, value
    if winner is None:
        return
    _emit_line(f"note: {key.dotted} is also set in {layer_label(winner)}, "
               f"which takes precedence (effective: {_plain(effective)})")


def run_set(*, key: str, value: str, scope: Layer | None,
            control_root: Path) -> int:
    try:
        settable = _settable(key)
        layer, target = _target(settable, scope, control_root)
        if layer == "project":
            _require_project_file(target, control_root)
        current = target.read_text() if target.is_file() else ""
        parsed = settable.parse(value)
        try:
            result = apply_set(current, settable.path, parsed)
        except YAMLError as err:
            raise ConfigWriteError(
                f"invalid YAML in {target.name}: {config_yaml_error(err)}") from err
        # Verify even when the text is unchanged: the write is accepted only if
        # imp's own reader reports the value back at the claimed path.
        with scratch_seam(scratch_dir(target),
                          target.name) as scratch:
            if not verify(scratch, result.text, settable.read_path, parsed):
                if not isinstance(parsed, str):
                    raise ConfigWriteError(refusal(target, settable.dotted))
                result = apply_set(current, settable.path,
                                   DoubleQuotedScalarString(parsed))
                if not verify(scratch, result.text, settable.read_path, parsed):
                    raise ConfigWriteError(refusal(target, settable.dotted))
            land(scratch, target)
    except ConfigWriteError as err:
        _emit_error(str(err))
        return 2
    except LoadError as err:
        _emit_error(err.message)
        return 2
    _emit_line(f"{layer_label(target)}: {settable.dotted}: "
               f"{_render(parsed)} {_suffix(result)}")
    _emit_shadow(settable, layer, control_root)
    return 0


def run_unset(*, key: str, scope: Layer | None,
              control_root: Path) -> int:
    try:
        settable = _settable(key)
        layer, target = _target(settable, scope, control_root)
        if layer == "project":
            _require_project_file(target, control_root)
        elif not target.is_file():
            _emit_line(f"{layer_label(target)}: {settable.dotted} "
                       "is not set (nothing to do)")
            return 0
        try:
            result = apply_unset(target.read_text(), settable.path)
        except YAMLError as err:
            raise ConfigWriteError(
                f"invalid YAML in {target.name}: {config_yaml_error(err)}") from err
        if result.outcome is UnsetOutcome.SHARED_LINE:
            raise ConfigWriteError(
                f"refusing to write {layer_label(target)} — {settable.dotted!r} "
                "shares its line with other settings; edit the file by hand")
        if result.outcome is UnsetOutcome.FLOW_BLOCKED:
            raise ConfigWriteError(
                f"refusing to write {layer_label(target)} — {settable.dotted!r} "
                "is inside a multi-line { } block; edit the file by hand")
        if result.outcome is UnsetOutcome.ABSENT:
            _emit_line(f"{layer_label(target)}: {settable.dotted} "
                       "is not set (nothing to do)")
            return 0
        with scratch_seam(scratch_dir(target),
                          target.name) as scratch:
            verify_readable(scratch, result.text)
            land(scratch, target)
    except ConfigWriteError as err:
        _emit_error(str(err))
        return 2
    except LoadError as err:
        _emit_error(err.message)
        return 2
    _emit_line(f"{layer_label(target)}: {settable.dotted} commented out "
               f"(was {_render(result.previous)})")
    _emit_shadow(settable, layer, control_root)
    return 0
