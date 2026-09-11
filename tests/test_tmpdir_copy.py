"""The repo's prose contract for the read-only `/tmp`. No shipped instruction tells
an agent to write there, and no comment still calls the host temp dir an identity
read-write bind."""
from __future__ import annotations

from imp.agents.skills import load_internal_skill
from imp.assets import bundled_skills_dir
from tests._corpus import tracked

_ROOTS = ("src/imp/assets/skills", "tests/playbooks", "scripts/showcase",
          "src/impty/README.md")
_FALLBACK = "${TMPDIR:-/tmp}"
# `make snap-ui` is a human-run helper that never executes inside the sandbox.
_ALLOWED = {
    ("src/impty/README.md",
     "`make snap-ui` writes `/tmp/imp_ui/stream.png` from the stored journal through"),
}


def _literal_tmp_lines() -> set[tuple[str, str]]:
    hits = set()
    for path in tracked(*_ROOTS, suffixes=(".md", ".py")):
        for line in path.read_text().splitlines():
            if "/tmp/" in line.replace(_FALLBACK, ""):
                hits.add((path.as_posix(), line.strip()))
    return hits


def test_no_shipped_instruction_writes_to_a_literal_tmp_path() -> None:
    # AC 12(a): every shipped instruction that writes scratch names $TMPDIR (or the
    # ${TMPDIR:-/tmp} fallback for a host-run shell), so none of them hands its
    # reader a path the sandbox refuses.
    assert _literal_tmp_lines() == _ALLOWED


def test_web_ui_skill_sends_screenshots_to_the_scratch_dir() -> None:
    # AC 12(a)/(c): both browser one-liners write to $TMPDIR, and the read_file
    # handoff names the no-expansion guard — resolve_path does not expand env vars,
    # so a literal "$TMPDIR/shot.png" would resolve under the project root.
    body = load_internal_skill(bundled_skills_dir(), "imp-driving-web-uis")
    norm = " ".join(body.split())
    assert "--screenshot=${TMPDIR:-/tmp}/shot.png" in norm
    assert "--screenshot ${TMPDIR:-/tmp}/shot.png" in norm
    assert "Write shots to `$TMPDIR`, never into the worktree" in norm
    assert ("Then **`read_file` the PNG at its literal path** — `read_file` is not "
            "a shell and won't expand `$TMPDIR`.") in norm
    assert ("one self-contained file with inline CSS, written to `$TMPDIR`, "
            "rendered and read back") in norm
    assert "Write throwaway driver scripts to `$TMPDIR` and run them there." in norm
    assert "screenshot to `$TMPDIR`, and print the console errors" in norm
