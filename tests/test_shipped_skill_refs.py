from __future__ import annotations

import re
from pathlib import Path

from tests._corpus import tracked

_CALL = re.compile(r'load_skill\(name="([^"]*)"\)')
_TERMINAL = 'load_skill(name="imp-driving-terminal-uis")'
_WEB = 'load_skill(name="imp-driving-web-uis")'

_REFERRERS = (
    "src/imp/assets/partials/brainstorm-dialogue.md",
    "src/imp/assets/workflows/default/agents/batch-code-reviewer.md",
    "src/imp/assets/workflows/default/agents/finalize.md",
    "src/imp/assets/workflows/default/agents/spiker.md",
    "src/imp/assets/workflows/minimal/agents/spiker.md",
)

# Each snippet starts inside the file's existing terminal-skill sentence, so a passing
# assertion proves the web reference sits adjacent to it, not merely somewhere in the file.
_ADJACENT: dict[str, tuple[str, ...]] = {
    "src/imp/assets/partials/brainstorm-dialogue.md": (
        "never depict a color the codes cannot produce. "
        "A web mockup is never ASCII — it is real HTML you render and "
        "hand the human a path to.",
        "grounded in the real rendering, not a guess. "
        "When it turns on a browser surface, load `imp-driving-web-uis` "
        '(`load_skill(name="imp-driving-web-uis")`) and render it the same way.',
    ),
    "src/imp/assets/workflows/default/agents/batch-code-reviewer.md": (
        "render the result with your impty tools before judging it — the diff won't show "
        "rendered output. **Browser-UI changes — see them.** When the change alters a "
        "browser-rendered surface, load `imp-driving-web-uis` "
        '(`load_skill(name="imp-driving-web-uis")`) and render the affected pages before '
        "judging them — neither the diff nor an HTML-string assertion shows a rendered page.",
    ),
    "src/imp/assets/workflows/default/agents/finalize.md": (
        "with your impty tools to describe what you see. For a browser surface, load "
        '`imp-driving-web-uis` (`load_skill(name="imp-driving-web-uis")`) instead.',
    ),
    "src/imp/assets/workflows/default/agents/spiker.md": (
        "and drive it on a pty with your impty tools to capture what it actually renders. "
        "- When the question is about a page's rendering or its in-browser behavior, load "
        '`imp-driving-web-uis` (`load_skill(name="imp-driving-web-uis")`) and render or '
        "drive it to capture what the browser actually does.",
    ),
}
_ADJACENT["src/imp/assets/workflows/minimal/agents/spiker.md"] = _ADJACENT[
    "src/imp/assets/workflows/default/agents/spiker.md"
]


def _shipped_md() -> list[Path]:
    found = tracked("src")
    assert found, "no tracked markdown found under src/"
    return found


def _flat(path: Path) -> str:
    return " ".join(path.read_text(encoding="utf-8").split())


def test_web_skill_reaches_exactly_the_terminal_skill_sites() -> None:
    texts = {str(p): p.read_text(encoding="utf-8") for p in _shipped_md()}
    web = {name for name, text in texts.items() if _WEB in text}
    terminal = {name for name, text in texts.items() if _TERMINAL in text}
    assert web == terminal
    assert web == set(_REFERRERS)
    # Occurrence parity, not just file parity: one file must not carry two web refs.
    assert (sum(t.count(_WEB) for t in texts.values())
            == sum(t.count(_TERMINAL) for t in texts.values())
            == len(_REFERRERS))


def test_each_referrer_carries_the_pinned_wording_beside_its_terminal_reference() -> None:
    for name, snippets in _ADJACENT.items():
        flat = _flat(Path(name))
        for snippet in snippets:
            assert snippet in flat, f"{name}: missing {snippet[:60]!r}"


def test_the_two_spiker_prompts_stay_byte_identical() -> None:
    default = Path("src/imp/assets/workflows/default/agents/spiker.md").read_bytes()
    minimal = Path("src/imp/assets/workflows/minimal/agents/spiker.md").read_bytes()
    assert default == minimal


_ASSETS = Path("src/imp/assets")
_WORKFLOWS = _ASSETS / "workflows"


def _skill_pools(prompt: Path) -> tuple[Path, ...]:
    """Global pool always; a workflow's own skills/ only for a prompt inside it."""
    pools = (_ASSETS / "skills",)
    if prompt.is_relative_to(_WORKFLOWS):
        workflow = prompt.relative_to(_WORKFLOWS).parts[0]
        pools += (_WORKFLOWS / workflow / "skills",)
    return pools


def _resolves(prompt: Path, name: str) -> bool:
    return any((pool / f"{name}.md").is_file() for pool in _skill_pools(prompt))


def _call_sites() -> list[tuple[str, str]]:
    return [(str(p), name)
            for p in _shipped_md()
            for name in _CALL.findall(p.read_text(encoding="utf-8"))]


def test_every_shipped_load_skill_name_resolves() -> None:
    sites = _call_sites()
    # Non-vacuity: the scan really finds the known call sites, both kinds of pool.
    assert ("src/imp/assets/partials/imp-grounding.md", "imp-knowledge") in sites
    assert ("src/imp/assets/workflows/default/agents/coder.md",
            "test-driven-development") in sites
    unresolved = [site for site in sites if not _resolves(Path(site[0]), site[1])]
    assert unresolved == [], f"shipped load_skill names that resolve to nothing: {unresolved}"


def test_an_unknown_skill_name_does_not_resolve() -> None:
    prompt = Path("src/imp/assets/workflows/default/agents/coder.md")
    assert _resolves(prompt, "test-driven-development")
    assert not _resolves(prompt, "no-such-skill-anywhere")


def test_a_workflow_local_skill_does_not_resolve_for_a_foreign_prompt() -> None:
    # test-driven-development lives only in workflows/default/skills/.
    assert _resolves(Path("src/imp/assets/workflows/default/agents/spiker.md"),
                     "test-driven-development")
    assert not _resolves(Path("src/imp/assets/workflows/minimal/agents/spiker.md"),
                         "test-driven-development")
    partial = Path("src/imp/assets/partials/brainstorm-dialogue.md")
    assert not _resolves(partial, "test-driven-development")
    assert _resolves(partial, "imp-driving-web-uis")
