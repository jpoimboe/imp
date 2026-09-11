"""The repo's prose contract for the read-only `/tmp`. No shipped instruction tells
an agent to write there, and no comment still calls the host temp dir an identity
read-write bind."""
from __future__ import annotations

import re
from pathlib import Path

from imp.agents.skills import load_internal_skill
from imp.assets import bundled_skills_dir
from tests._corpus import tracked

# AC 6(b)'s grep: `[^.]` spans newlines, so a claim wrapped across two lines is
# caught the same as a one-liner.
_IDENTITY = re.compile(r"(/tmp[^.]{0,120}identity|identity[^.]{0,120}/tmp)")

_ROOTS = ("src/imp/assets/skills", "tests/playbooks", "scripts/showcase",
          "src/impty/README.md")
_FALLBACK = "${TMPDIR:-/tmp}"
# `make snap-ui` is a human-run helper that never executes inside the sandbox;
# match the path token so innocent rewording of surrounding prose doesn't break this.
_ALLOWED = {
    ("src/impty/README.md", "/tmp/imp_ui/stream.png"),
}


def _literal_tmp_lines() -> set[tuple[str, str]]:
    # Bare /tmp (no trailing slash) counts — the migrated copy said "Write shots to /tmp".
    # Lines containing "read-only" are exempt: they state the rule, not instruct a write.
    hits = set()
    for path in tracked(*_ROOTS, suffixes=(".md", ".py")):
        for line in path.read_text().splitlines():
            stripped = line.replace(_FALLBACK, "")
            if "/tmp" in stripped and "read-only" not in stripped:
                hits.add((path.as_posix(), line.strip()))
    return hits


def test_no_shipped_instruction_writes_to_a_literal_tmp_path() -> None:
    # AC 12(a): every shipped instruction that writes scratch names $TMPDIR (or the
    # ${TMPDIR:-/tmp} fallback for a host-run shell), so none of them hands its
    # reader a path the sandbox refuses. Lines stating /tmp is read-only are exempt
    # (they explain the rule); known human-only helpers are exempt by path token.
    hits = _literal_tmp_lines()
    allowed = {h for h in hits
               if any(h[0] == p and token in h[1] for p, token in _ALLOWED)}
    assert hits - allowed == set()
    assert all(any(h[0] == p and token in h[1] for h in hits)
               for p, token in _ALLOWED), f"dead exemption in _ALLOWED: {_ALLOWED}"


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


def test_no_source_prose_claims_host_tmp_is_an_identity_rw_bind() -> None:
    # AC 6(b): host /tmp is on the read-only floor now; the only bind at its real
    # path is the quest scratch dir. Nothing may still describe the old contract.
    me = Path(__file__).resolve()   # this test's own name+comment spans the pattern
    hits = [(p.as_posix(), m)
            for p in tracked("src/imp", "tests", suffixes=(".py", ".md"))
            if p.resolve() != me
            for m in _IDENTITY.findall(p.read_text())]
    assert hits == []


def test_impty_readme_describes_the_readonly_tmp_as_shipped() -> None:
    # AC 12(b): the rebind this quest ships is no longer described as future work.
    norm = " ".join(Path("src/impty/README.md").read_text().split())
    assert "a future `/tmp` rebind" not in norm
    assert ("so the sandbox's read-only `/tmp` — only the per-quest scratch dir is "
            "bound writable — leaves them where they are.") in norm
