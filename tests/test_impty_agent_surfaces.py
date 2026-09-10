"""The surfaces an agent reads BEFORE it picks a `hard`: the thin one-liner both
provider hosts show for each impty tool, the imp-driving-terminal-uis skill, and
the sandbox copy that now has to account for programs driven on a terminal."""

from __future__ import annotations

from pathlib import Path

from imp.agents.skills import load_internal_skill
from imp.assets import bundled_partials_dir, bundled_skills_dir
from impty.driver import ImptyDriver
from impty.mcp import THIN_DESCRIPTIONS
from impty.session import MAX_CALL_HARD


def test_wait_one_liner_states_the_per_call_cap_and_the_resume_instruction() -> None:
    # The full driver docstring is agent-facing only on the standalone stdio
    # server, so this one-liner is all the agent sees under both providers.
    desc = THIN_DESCRIPTIONS["wait"]
    assert f"{MAX_CALL_HARD:g}s" in desc
    assert "call again" in desc


def test_terminal_ui_skill_wait_bullet_states_the_clamp() -> None:
    body = load_internal_skill(bundled_skills_dir(), "imp-driving-terminal-uis")
    # Slicing the bullet proves the sentence sits on `wait`, not merely somewhere
    # in the skill.
    bullet = next((b for b in body.split("\n- ") if b.startswith("**`wait(")), None)
    assert bullet is not None, "the skill has no `wait(` bullet"
    assert f"{MAX_CALL_HARD:g}s" in bullet
    assert "call again" in bullet


def test_terminal_ui_skill_closes_by_telling_the_agent_to_paste_the_screen() -> None:
    body = load_internal_skill(bundled_skills_dir(), "imp-driving-terminal-uis")
    parts = body.split("## Showing the human", 1)
    assert len(parts) == 2, "the skill has no `Showing the human` section"
    # Slicing the section proves the rule sits under its own heading, and the
    # end-anchor proves it is the closing section the spec asks for.
    section = " ".join(parts[1].split())
    assert section.startswith(
        "Paste the screen into your message as a fenced block: color survives there."
    )
    # An agent cannot type ESC, so the bare spelling is the load-bearing instruction:
    # without it the skill describes a feature no model can reach.
    assert "write each SGR code without it — `[1;33m`" in section
    # The capability itself: without this sentence the skill still tells the agent
    # nothing outside a fence is restored, and no model would try an inline sample.
    assert ("imp restores it inside the fence, and inside a single-line inline code "
            "span, so a short sample like `` `[1;33m78%[0m` `` keeps its color "
            "mid-sentence") in section
    assert "Outside a fence nothing is restored" not in section
    assert "any control byte that isn't SGR arrives as literal `^[[33m`" in section
    assert " ".join(body.split()).endswith("give the snapshot PNG path too.")


def test_terminal_ui_skill_view_bullet_describes_the_exit_status() -> None:
    body = load_internal_skill(bundled_skills_dir(), "imp-driving-terminal-uis")
    bullet = next((b for b in body.split("\n- ") if b.startswith("**`view(")), None)
    assert bullet is not None, "the skill has no `view(` bullet"
    assert "exit=0" in bullet
    assert "exit=SIGINT" in bullet


def test_changed_rows_contract_reaches_every_agent_surface() -> None:
    def normalized(text: str) -> str:
        return " ".join(text.split())

    def require(text: str, phrase: str) -> None:
        assert phrase in normalized(text)

    def tool_bullet(section: str, name: str) -> str:
        bullet = next(
            (part for part in section.split("\n- ") if part.startswith(f"**`{name}(")),
            None,
        )
        assert bullet is not None, f"the tools section has no `{name}` bullet"
        return normalized(bullet)

    def markdown_bullet(text: str, title: str) -> str:
        lines = text.splitlines(keepends=True)
        start = next(
            (index for index, line in enumerate(lines) if line.startswith(f"- **{title}:**")),
            None,
        )
        assert start is not None, f"the README has no `{title}` bullet"
        end = next(
            (
                index
                for index, line in enumerate(lines[start + 1 :], start + 1)
                if line.startswith(("- ", "#"))
            ),
            len(lines),
        )
        return "".join(lines[start:end])

    union = (
        "return a changed-row diff, `[no text change]`, or `[full screen]` "
        "followed by the complete screen"
    )
    thin_union = (
        "return a changed-row diff, `[no text change]`, or `[full screen]` "
        "plus the complete screen."
    )
    driver = ImptyDriver()
    docs = {method.__name__: normalized(method.__doc__ or "") for method in driver.tool_methods()}
    render = normalized(driver._render.__doc__ or "")

    require(render, "complete screen (or whole strip)")
    require(render, "without a diff or `[full screen]` marker")
    require(render, "resynchronizes the changed-row baseline")
    require(docs["start"], "settles, exits, or the 3s settle deadline elapses")
    require(docs["start"], "complete screen accumulated so far")
    require(docs["start"], "return `[full screen]` followed by")
    require(docs["start"], "startup screen can be incomplete")
    require(docs["start"], "clears the changed-row baseline")
    for stale in (
        "return its first screen",
        "returned screen is the first thing",
        "Waits for the program to settle before returning",
    ):
        assert stale not in docs["start"]
    for name in ("send", "resize", "wait", "send_signal"):
        require(docs[name], union)
        for denial in ("never `[full screen]`", "does not return `[full screen]`"):
            assert denial not in docs[name]
    require(docs["send"], "`wait=False` returns `[sent]` without rendering")
    require(docs["resize"], "`wait=False` returns `[resized]` without rendering")
    require(
        docs["wait"],
        "idle window after qualifying output, the process exits, or `hard` elapses",
    )
    require(docs["wait"], "Output already read qualifies normally")
    require(docs["wait"], "`for_change=True` requires new output")
    require(docs["wait"], "live process with no output is not settled")
    require(docs["wait"], "`hard` defaults to 3s (30s with `for_change=True`)")
    require(docs["wait"], "values over 240s are capped")
    require(docs["view"], "complete current screen without a diff or `[full screen]` marker")
    require(docs["view"], "resynchronizes subsequent changed-row responses")

    expected_thin = {
        "start": (
            "Spawn a program on a fresh pseudo-terminal; wait until it settles, exits, or "
            "the 3s settle deadline elapses, then return `[full screen]` followed by the "
            "complete screen accumulated so far."
        ),
        "view": (
            "Read the complete current screen without a diff or `[full screen]` marker; "
            "resynchronizes changed-row responses."
        ),
        "send": (
            "Send raw keystrokes; `wait=False` returns `[sent]` without rendering, otherwise "
            "return a changed-row diff, `[no text change]`, or `[full screen]` plus the "
            "complete screen."
        ),
        "resize": (
            "Resize the terminal (SIGWINCH); `wait=False` returns `[resized]` without "
            "rendering, otherwise return a changed-row diff, `[no text change]`, or `[full "
            "screen]` plus the complete screen."
        ),
        "wait": (
            "Wait until there is an idle window after qualifying output, the process exits, "
            "or `hard` elapses; then return a changed-row diff, `[no text change]`, or `[full "
            "screen]` plus the complete screen. Output already read qualifies by default; "
            "`for_change=True` requires new output. `hard` defaults to 3s (30s with "
            "`for_change=True`), and values over 240s are capped; call again to keep watching."
        ),
    }
    for name, expected in expected_thin.items():
        assert THIN_DESCRIPTIONS[name] == expected
    for name in ("send", "resize", "wait"):
        require(THIN_DESCRIPTIONS[name], thin_union)
    assert THIN_DESCRIPTIONS["send_signal"] == (
        "Send a real signal (default SIGINT) to the terminal's foreground process group."
    )
    for stale in ("return its first screen", "return the new screen", "return the reflowed screen"):
        assert stale not in " ".join(THIN_DESCRIPTIONS.values())

    skill = load_internal_skill(bundled_skills_dir(), "imp-driving-terminal-uis")
    tools_section = skill.split("## The tools", 1)[1].split("## The loop", 1)[0]
    start = tool_bullet(tools_section, "start")
    view = tool_bullet(tools_section, "view")
    send = tool_bullet(tools_section, "send")
    wait = tool_bullet(tools_section, "wait")
    resize = tool_bullet(tools_section, "resize")
    send_signal = tool_bullet(tools_section, "send_signal")
    require(start, "3s settle deadline")
    require(start, "complete screen accumulated so far")
    require(start, "startup screen can be incomplete")
    require(start, "return `[full screen]` followed by")
    require(view, "complete current screen without a diff or `[full screen]` marker")
    require(view, "resynchronizes subsequent changed-row responses")
    require(send, "`wait=False` returns `[sent]` without rendering")
    require(resize, "`wait=False` returns `[resized]` without rendering")
    for bullet in (send, wait, resize, send_signal):
        require(bullet, union)
        for denial in ("never `[full screen]`", "does not return `[full screen]`"):
            assert denial not in bullet
    require(wait, "idle window after qualifying output, the process exits, or `hard` elapses")
    require(wait, "already-read output qualifies normally")
    require(wait, "`for_change=True` requires new output")
    require(wait, "live process with no output is not settled")
    require(wait, "`hard` defaults to 3s (30s with `for_change=True`)")
    require(wait, "caller values are accepted")
    require(wait, "values over 240s are capped")
    require(
        tools_section,
        "Diff hunks use 0-based labels such as `[row 7]`, `[rows 20-22 inserted]`, "
        "and `[3 rows deleted at row 0]`. Call `view()` to resynchronize.",
    )
    assert "returns the first screen" not in tools_section

    readme = Path("src/impty/README.md").read_text()
    live_mcp = markdown_bullet(readme, "Drive a live program over MCP and screenshot it")
    require(live_mcp, "3s settle deadline")
    require(live_mcp, "complete screen accumulated so far")
    require(live_mcp, "returns `[full screen]` followed by")
    require(live_mcp, "`wait(hard=...)` then `view()`")
    require(live_mcp, "`send(wait=False)` returns `[sent]` without rendering")
    require(live_mcp, "`resize(..., wait=False)` returns `[resized]` without rendering")
    require(
        live_mcp,
        "otherwise `send`, `resize`, `wait`, and `send_signal` return a changed-row diff "
        "with 0-based labels, `[no text change]`, or `[full screen]` plus the complete screen",
    )
    require(live_mcp, "complete current screen with no diff or `[full screen]` marker")
    require(live_mcp, "resynchronizes subsequent changed-row responses")
    require(live_mcp, "`full=True` returns the whole strip")
    require(live_mcp, "`exit=0`")
    require(live_mcp, "`exit=SIGINT`")
    require(live_mcp, "terminal's foreground process group")
    require(live_mcp, "`rows, cols`")
    require(live_mcp, "real `SIGWINCH`")
    require(live_mcp, "`wait(for_change=True)` waits for NEW output")
    require(live_mcp, "`snapshot` renders the live screen to a PNG")
    require(live_mcp, "`stop` sends SIGTERM")
    require(live_mcp, "server restart")
    for denial in ("never `[full screen]`", "does not return `[full screen]`"):
        assert denial not in live_mcp


def test_stop_one_liner_states_the_graceful_ladder() -> None:
    desc = THIN_DESCRIPTIONS["stop"]
    assert "gracefully" in desc
    assert "SIGTERM" in desc and "SIGKILL" in desc


def test_terminal_ui_skill_stop_bullet_describes_the_ladder_and_the_return() -> None:
    body = load_internal_skill(bundled_skills_dir(), "imp-driving-terminal-uis")
    bullet = next((b for b in body.split("\n- ") if b.startswith("**`stop(")), None)
    assert bullet is not None, "the skill has no `stop(` bullet"
    assert "SIGTERM" in bullet and "SIGKILL" in bullet
    assert "exit status and final screen" in bullet


def test_terminal_ui_skill_start_bullet_states_the_env_overlay() -> None:
    body = load_internal_skill(bundled_skills_dir(), "imp-driving-terminal-uis")
    bullet = next((b for b in body.split("\n- ") if b.startswith("**`start(")), None)
    assert bullet is not None, "the skill has no `start(` bullet"
    assert ("`env` entries are ADDED to the environment, overriding same-named ones "
            "(TERM\n  still defaulted).") in bullet
    # AC 13(b), on the whole skill: the claim must not survive anywhere in it.
    assert "*replaces* the environment" not in body


def test_terminal_ui_skill_send_signal_bullet_names_the_foreground_group() -> None:
    body = load_internal_skill(bundled_skills_dir(), "imp-driving-terminal-uis")
    bullet = next((b for b in body.split("\n- ") if b.startswith("**`send_signal(")),
                  None)
    assert bullet is not None, "the skill has no `send_signal(` bullet"
    assert "deliver a real signal to the terminal's foreground" in bullet
    assert "in a shell it\n  reaches the running job, not the shell." in bullet
    # The old aim, which a joined session makes actively wrong. Scoped to THIS
    # bullet: stop()'s ladder really does keep the direct child's own group, so
    # its "SIGTERM the program's group" stays true and is asserted below rather
    # than left to look like an oversight.
    assert "the program's group" not in bullet
    stop_bullet = next((b for b in body.split("\n- ") if b.startswith("**`stop(")),
                       None)
    assert stop_bullet is not None and "SIGTERM the program's group" in stop_bullet


def test_terminal_ui_skill_states_the_program_shares_the_runs_sandbox() -> None:
    body = load_internal_skill(bundled_skills_dir(), "imp-driving-terminal-uis")
    # Anchored to the snapshots line, so the paragraph sits where an agent reading
    # the loop actually meets it rather than merely somewhere in the file.
    assert (
        "Snapshots work even where the worktree is read-only.\n\n"
        "The program runs in the same sandbox as your `bash` calls and shares their pid\n"
        "namespace: `ps` in a bash call sees it and can kill it, masked paths stay masked,\n"
        "and everything it spawns dies when you finish. `create_and_enter_worktree`\n"
        "rebuilds that sandbox and so ends the session — `start` again after it.\n"
    ) in body


def test_tool_reference_pid_namespace_bullet_covers_terminal_sessions() -> None:
    body = load_internal_skill(bundled_skills_dir(), "imp-tool-reference")
    assert (
        "running and a pid from an earlier call is still valid — including a program you\n"
        "  left running on a terminal session. Everything in it is\n"
    ) in body


def test_sandbox_limits_partial_covers_a_program_driven_on_a_terminal() -> None:
    body = (bundled_partials_dir() / "sandbox-limits.md").read_text()
    assert (
        "imp's sandbox runs all your shell commands — and any program you drive on a\n"
        "terminal — in one user and pid namespace of their own, so an agent cannot attach\n"
    ) in body
    # /sandbox off drops the sandbox for the pty path too, by the same seam.
    assert "so shell, file **and terminal** tools run as" in body
    assert "imp's bash sandbox runs all your shell commands" not in body


def test_architecture_doc_live_drivers_row_states_the_sandbox_join() -> None:
    row = next(ln for ln in Path("docs/internals/architecture.md").read_text().splitlines()
               if ln.startswith("| Live drivers |"))
    # "launched self-hosted" reads as "outside the sandbox", which is exactly the
    # hole this quest closed.
    assert "launched self-hosted" not in row
    assert "join the run's sandbox namespace" in row


def test_no_shipped_copy_still_claims_the_old_env_and_signal_behavior() -> None:
    # AC 13(b) as a sweep over every shipped skill and partial, not a spot check:
    # a survivor teaches an agent the wrong contract. The exact phrasings in play,
    # so the sweep is red today rather than vacuously green — and narrow enough
    # that stop()'s legitimate "SIGTERM the program's group" is not caught.
    stale = ("*replaces* the environment", "REPLACES the inherited environment",
             "signal to the program's group")
    for directory in (bundled_skills_dir(), bundled_partials_dir()):
        for path in sorted(directory.rglob("*.md")):
            flat = " ".join(path.read_text().split())
            for claim in stale:
                assert claim not in flat, f"{path.name} still says {claim!r}"
