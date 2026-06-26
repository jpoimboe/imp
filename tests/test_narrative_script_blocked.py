from __future__ import annotations

from imp.cli import narrative as nv
from imp.cli.narrative import NarrativeBuilder
from imp.cli.render_text import render_tree


def _error_nodes(events: list[dict]) -> list[nv.ErrorNode]:
    nb = NarrativeBuilder()
    for e in events:
        nb.add(e)
    found: list[nv.ErrorNode] = []

    def _walk(node: object) -> None:
        if isinstance(node, nv.ErrorNode):
            found.append(node)
        for child in getattr(node, "children", []):
            _walk(child)

    _walk(nb.tree.root)
    return found


def test_script_blocked_handler_registered() -> None:
    assert "script_blocked" in NarrativeBuilder._HANDLERS


def test_script_blocked_appends_error_node_with_step_exit_and_stderr() -> None:
    errors = _error_nodes([
        {"type": "script_blocked",
         "payload": {"step": "cleanup-merge", "exit": 3,
                     "stdout": "", "stderr": "fixup! commit still present"}},
    ])
    assert len(errors) == 1
    node = errors[0]
    assert node.category == "script"
    assert node.is_warning is False
    assert "cleanup-merge" in node.message
    assert "exit 3" in node.message
    assert "fixup! commit still present" in node.detail


def test_script_blocked_omits_exit_suffix_when_code_not_int() -> None:
    errors = _error_nodes([
        {"type": "script_blocked",
         "payload": {"step": "cleanup-merge", "exit": None,
                     "stdout": "", "stderr": "boom"}},
    ])
    assert "exit" not in errors[0].message
    assert errors[0].message.startswith("cleanup-merge blocked")


def test_script_blocked_appends_stdout_after_stderr_in_detail() -> None:
    errors = _error_nodes([
        {"type": "script_blocked",
         "payload": {"step": "cleanup-merge", "exit": 1,
                     "stdout": "tail of stdout", "stderr": "stderr first"}},
    ])
    detail = errors[0].detail
    assert detail.index("stderr first") < detail.index("tail of stdout")


def test_script_blocked_detail_is_stdout_when_stderr_empty() -> None:
    errors = _error_nodes([
        {"type": "script_blocked",
         "payload": {"step": "cleanup-merge", "exit": 1,
                     "stdout": "only stdout here", "stderr": ""}},
    ])
    assert errors[0].detail == "only stdout here"


def test_script_blocked_renders_error_above_paused_line() -> None:
    tree = nv.build_tree([
        {"type": "script_blocked",
         "payload": {"step": "cleanup-merge", "exit": 1,
                     "stdout": "", "stderr": "fixup! commit still present"}},
        {"type": "workflow_paused", "payload": {"reason": "script_blocked"}},
    ])
    out = list(render_tree(tree, full=True, color=False, width=80))
    err_idx = next(i for i, ln in enumerate(out)
                   if "error: [script] cleanup-merge blocked (exit 1)" in ln)
    detail_idx = next(i for i, ln in enumerate(out)
                      if "fixup! commit still present" in ln)
    paused_idx = next(i for i, ln in enumerate(out) if "paused (" in ln)
    assert err_idx < detail_idx < paused_idx


def test_lean_script_block_shows_detail() -> None:
    tree = nv.build_tree([
        {"type": "script_blocked",
         "payload": {"step": "build-filing", "exit": 2,
                     "stdout": "", "stderr": "DeploymentException: bootstrap failed"}},
    ])
    out = "\n".join(render_tree(tree, full=False, color=False, width=80))
    assert "error: [script] build-filing blocked (exit 2)" in out
    assert "DeploymentException: bootstrap failed" in out


def test_lean_non_script_error_hides_detail() -> None:
    tree = nv.build_tree([
        {"type": "error",
         "payload": {"message": "boom", "category": None,
                     "detail": {"full_summary": "stack trace details"}}},
    ])
    out = "\n".join(render_tree(tree, full=False, color=False, width=80))
    assert "error: boom" in out
    assert "stack trace details" not in out


def test_lean_warning_with_detail_hides_detail() -> None:
    nb = NarrativeBuilder()
    nb._append(nv.ErrorNode(message="heads up", is_warning=True, detail="hidden body"))
    out = "\n".join(render_tree(nb.tree, full=False, color=False, width=80))
    assert "warning: heads up" in out
    assert "hidden body" not in out
