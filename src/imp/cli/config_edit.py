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


class UnsetOutcome(Enum):
    COMMENTED = "commented"
    ABSENT = "absent"
    SHARED_LINE = "shared_line"
    FLOW_BLOCKED = "flow_blocked"


@dataclass(frozen=True, slots=True)
class UnsetResult:
    text: str
    outcome: UnsetOutcome
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
    pattern = re.compile(rf"^#[ ]?{re.escape(key)}:")
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


def _navigate(doc: Any, segs: tuple[str, ...]) -> Any:
    node = doc
    for seg in segs:
        if not isinstance(node, dict) or seg not in node:
            return None
        node = node[seg]
    return node if isinstance(node, dict) else None


def _region_end(doc: Any, path: tuple[str, ...], total: int) -> int:
    """First line past the key's own region: the next key at its own level or at
    any shallower one, else EOF. Indentation cannot answer this — a block
    sequence (`providers:` / `- anthropic`) and a wrapped flow sequence's closing
    bracket both sit at the key's own column."""
    end = total
    node: Any = doc
    for depth in range(len(path)):
        keys = list(node)
        index = keys.index(path[depth])
        if index + 1 < len(keys):
            end = min(end, node.lc.key(keys[index + 1])[0])
        node = node[path[depth]]
    return end


def _comment_out(text: str, doc: Any, path: tuple[str, ...]) -> str:
    """Prefix every line of the key's region with `# `, releasing trailing blank
    and comment lines back to the next key."""
    lines = text.splitlines(keepends=True)
    parent = _navigate(doc, path[:-1])
    start = parent.lc.key(path[-1])[0]
    end = _region_end(doc, path, len(lines))
    while end - 1 > start and not _is_live(lines[end - 1]):
        end -= 1
    for index in range(start, end):
        if lines[index].strip():
            lines[index] = "# " + lines[index]
    return "".join(lines)


def _shares_its_line(parent: Any, key: str) -> bool:
    line = parent.lc.key(key)[0]
    return any(parent.lc.key(other)[0] == line for other in parent if other != key)


def _flow_spans_other_lines(parent: Any, key: str) -> bool:
    """A flow mapping commented line-wise only survives when the whole `{ … }`
    sits on the key's own line: anything else strands a brace or a sibling."""
    if not parent.fa.flow_style():
        return False
    line = parent.lc.key(key)[0]
    return (parent.lc.line != line
            or any(parent.lc.key(other)[0] != line for other in parent))


def apply_unset(text: str, path: tuple[str, ...]) -> UnsetResult:
    doc = _rt().load(text)
    parent = _navigate(doc, path[:-1])
    if parent is None or path[-1] not in parent:
        return UnsetResult(text, UnsetOutcome.ABSENT)
    if _shares_its_line(parent, path[-1]):
        return UnsetResult(text, UnsetOutcome.SHARED_LINE)
    if _flow_spans_other_lines(parent, path[-1]):
        return UnsetResult(text, UnsetOutcome.FLOW_BLOCKED)
    previous = parent[path[-1]]
    out = _comment_out(text, doc, path)
    # _parse_tiers rejects an empty tier mapping, so a parent left with no live
    # children must go too, or the file no longer loads.
    for depth in range(len(path) - 1, 0, -1):
        parent_path = path[:depth]
        reloaded = _rt().load(out)
        holder = _navigate(reloaded, parent_path[:-1])
        if holder is None or parent_path[-1] not in holder:
            continue      # that level went with the span; keep climbing
        if holder[parent_path[-1]]:
            break
        out = _comment_out(out, reloaded, parent_path)
    return UnsetResult(out, UnsetOutcome.COMMENTED, previous)


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
