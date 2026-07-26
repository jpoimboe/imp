"""Text→text edits to one config file. ruamel renders values and round-trips the
parse; line placement is done on text, so `set` and `unset` stay in one register."""

from __future__ import annotations

import io
import re
from dataclasses import dataclass
from enum import Enum
from typing import Any

from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap, CommentedSeq
from ruamel.yaml.error import YAMLError
from ruamel.yaml.scalarstring import DoubleQuotedScalarString


class SetOutcome(Enum):
    UPDATED = "updated"
    UNCOMMENTED = "uncommented"
    NEW = "new"
    UNCHANGED = "unchanged"


@dataclass(frozen=True, slots=True)
class SetResult:
    text: str
    outcome: SetOutcome
    previous: Any = None


def _rt() -> YAML:
    yaml = YAML(typ="rt")
    yaml.preserve_quotes = True
    yaml.width = 4096
    return yaml


def _flow(value: Any) -> Any:
    """ruamel's default for a plain list is a block sequence; config lists stay
    on one line."""
    if isinstance(value, list):
        seq = CommentedSeq(value)
        seq.fa.set_flow_style()
        return seq
    return value


def render_pair(path: tuple[str, ...], value: Any) -> str:
    """The `key: value` (or nested block) text ruamel emits for one setting."""
    node: Any = _flow(value)
    for seg in reversed(path):
        holder = CommentedMap()
        holder[seg] = node
        node = holder
    buf = io.StringIO()
    _rt().dump(node, buf)
    return buf.getvalue()


def _dump(doc: Any) -> str:
    buf = io.StringIO()
    _rt().dump(doc, buf)
    return buf.getvalue()


def _kind(value: Any) -> str:
    for typ, name in ((bool, "bool"), (int, "int"), (float, "float"),
                      (str, "str"), (list, "list"), (dict, "dict")):
        if isinstance(value, typ):
            return name
    return "none"


def _ruamel_target(doc: Any, path: tuple[str, ...]) -> Any:
    """The live mapping to assign into, creating any absent intermediate mapping
    inside the deepest live ancestor. None only when text placement owns the
    write: a top-level key that is not live (rules 2 and 3), or an intermediate
    that is not a mapping at all — a file imp already cannot read at this path."""
    if not isinstance(doc, CommentedMap):
        return None
    if path[0] not in doc:
        return None
    if len(path) == 1:
        return doc
    node: Any = doc
    for seg in path[:-1]:
        nxt = node.get(seg)
        if nxt is None:
            node[seg] = CommentedMap()
            nxt = node[seg]
        elif not isinstance(nxt, dict):
            return None
        node = nxt
    return node


def _reclaim_comment(text: str, path: tuple[str, ...], value: Any) -> str | None:
    """Rule 2: fill a single-line commented-out top-level placeholder. The
    candidate is the LAST matching line whose uncommented remainder parses as a
    value of the same kind — init's `# pythonpath: worktree-relative …` teaching
    line matches the pattern too, and must not be overwritten. Reviving the line
    through the round-trip loader and replacing only its value keeps whatever
    else it carried: `# theme: dark  # dark | light | ansi` comes back as
    `theme: light  # dark | light | ansi`."""
    if len(path) != 1:
        return None
    key = path[0]
    pattern = re.compile(rf"^#\s*{re.escape(key)}:")
    lines = text.splitlines(keepends=True)
    target: int | None = None
    rendered = ""
    for index, line in enumerate(lines):
        if not pattern.match(line):
            continue
        try:
            revived = _rt().load(line[1:])
        except YAMLError:
            continue
        if (isinstance(revived, dict) and key in revived
                and _kind(revived[key]) == _kind(value)):
            revived[key] = _flow(value)
            target, rendered = index, _dump(revived)
    if target is None:
        return None
    lines[target] = rendered
    return "".join(lines)


def _insert(text: str, path: tuple[str, ...], value: Any) -> str:
    """Rule 3: after the last live top-level key's final value line, above any
    trailing comment-only blocks."""
    lines = text.splitlines(keepends=True)
    index = len(lines) - 1
    while index >= 0 and not _is_live(lines[index]):
        index -= 1
    at = index + 1
    if at > 0 and not lines[at - 1].endswith("\n"):
        lines[at - 1] += "\n"
    return "".join([*lines[:at], render_pair(path, value), *lines[at:]])


def _is_live(line: str) -> bool:
    return bool(line.strip()) and not line.lstrip().startswith("#")


def apply_set(text: str, path: tuple[str, ...], value: Any) -> SetResult:
    doc = _rt().load(text)
    holder = _ruamel_target(doc, path)
    if holder is not None:
        leaf = path[-1]
        if leaf in holder:
            previous = holder[leaf]
            # An explicit requote is never a no-op: DoubleQuotedScalarString
            # compares equal to the plain string it must replace.
            if previous == value and not isinstance(value, DoubleQuotedScalarString):
                return SetResult(text, SetOutcome.UNCHANGED, previous)
            holder[leaf] = _flow(value)
            return SetResult(_dump(doc), SetOutcome.UPDATED, previous)
        holder.fa.set_block_style()   # a key sharing a flow line cannot be unset
        holder[leaf] = _flow(value)
        return SetResult(_dump(doc), SetOutcome.NEW)
    reclaimed = _reclaim_comment(text, path, value)
    if reclaimed is not None:
        return SetResult(reclaimed, SetOutcome.UNCOMMENTED)
    return SetResult(_insert(text, path, value), SetOutcome.NEW)
