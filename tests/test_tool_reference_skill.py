from __future__ import annotations

from imp.agents.skills import load_internal_skill
from imp.assets import bundled_skills_dir


def _skills_dir():
    return bundled_skills_dir()


def test_tool_reference_loads_via_internal_path_without_frontmatter() -> None:
    body = load_internal_skill(_skills_dir(), "imp-tool-reference")
    assert body.strip(), "imp-tool-reference skill missing/empty"
    assert not body.lstrip().startswith("---"), "skill must carry no frontmatter"


def test_tool_reference_describes_bash_promotion() -> None:
    body = load_internal_skill(_skills_dir(), "imp-tool-reference")
    # AC 10: the skill's bash section no longer says a foreground command is
    # "reaped after 120 s" — it describes promotion to a background task.
    assert "reaped after 120" not in body
    assert "promoted to a background task" in body


def test_tool_reference_holds_relocated_reference_detail() -> None:
    body = load_internal_skill(_skills_dir(), "imp-tool-reference")
    # One sentinel per tool whose description relocated detail here (AC 3):
    # every removed reference detail must still be present, only relocated.
    for sentinel in (
        "PNG/JPEG",              # read_file image handling
        "only the first 5 MB",   # read_file / edit_file caps
        "not valid UTF-8",       # edit_file refusal semantics
        "all-or-nothing",        # edit_file batch mechanics
        "DOTALL+MULTILINE",      # grep multiline
        "external-directory",    # grep walk cap
        "pathlib glob semantics",  # glob vs grep detail
        "most-recent-first",     # glob ordering
        "max_depth",             # symbol_outline
        "breadth-first thinning",  # symbol_outline
        "block-buffer",          # bash / bash_output buffering
        "out_path",              # bash background fields
        "err_path",
        "20-minute wait budget",  # bash_output budget/clip
        "advances the output cursors",  # bash_kill
    ):
        assert sentinel in body, f"imp-tool-reference missing: {sentinel!r}"


def test_tool_reference_describes_line_clipping_and_byte_exactness() -> None:
    # AC 6b: the bash bullet leads with clip-then-cap and the byte-exactness
    # invariant, and keeps its interruptibility/deadline text. Asserted against
    # whitespace-normalized prose: the sentences are hard-wrapped in the markdown,
    # so a raw-substring assertion would break on the line breaks (and on any
    # future rewrap).
    body = " ".join(load_internal_skill(_skills_dir(), "imp-tool-reference").split())
    assert "each line over 2000 chars is clipped to 2000" in body
    assert "the two streams are cut to a combined 30 KB head" in body
    assert "no `notice` means the output is byte-exact" in body
    assert "Foreground commands are interruptible" in body


def test_tool_reference_documents_sandbox_note() -> None:
    # The bash section names the denial field and its foreground-only scope.
    # Whitespace-normalized: the sentence is hard-wrapped in the markdown.
    body = " ".join(load_internal_skill(_skills_dir(), "imp-tool-reference").split())
    assert ("A denied write (a sandbox-masked path) surfaces the raw OS error "
            "plus a `sandbox_note` naming the boundary — foreground only; "
            "retrying never helps.") in body


def test_tool_reference_documents_per_call_pid_namespaces() -> None:
    # The bash section warns that pids are namespace-local and names bash_kill(id) as
    # the way to stop a background task. Whitespace-normalized: the prose is wrapped.
    body = " ".join(load_internal_skill(_skills_dir(), "imp-tool-reference").split())
    assert ("Each call gets its own pid namespace, so `ps` shows only that call's own "
            "processes and a pid recorded in one call means nothing in the next.") in body
    assert ("The `pid` in a background descriptor is a host-side handle — a shell command "
            "cannot signal it; stop a background task with `bash_kill(id)`.") in body


def _write_file_section() -> str:
    body = load_internal_skill(_skills_dir(), "imp-tool-reference")
    section = body.split("## write_file", 1)[1].split("\n## ", 1)[0]
    return " ".join(section.split())


def test_write_file_section_states_the_gate_applies_to_existing_files() -> None:
    assert ("overwriting an EXISTING file needs a `read_file` of it earlier in "
            "this step, unchanged since") in _write_file_section()


def test_write_file_section_says_any_window_discharges_the_gate() -> None:
    section = _write_file_section()
    assert "Any window discharges it" in section
    assert "`limit=5` on a huge file is enough" in section


def test_write_file_section_says_read_state_is_per_step() -> None:
    assert ("Read state is per step: a re-entered step starts empty, so revising "
            "an artifact your own earlier invocation wrote still needs a fresh "
            "`read_file`.") in _write_file_section()


def test_write_file_section_says_bash_writes_do_not_update_the_record() -> None:
    section = _write_file_section()
    assert ("`bash` writes (`cat >`, `tee`, `sed -i`) don't update the read record"
            in section)
    assert "Never use a shell redirect to get around a refusal." in section
