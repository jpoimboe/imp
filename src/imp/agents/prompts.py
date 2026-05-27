"""Prompt template rendering for agent .md bodies.

Three namespaces (spec §7.5):
  events.<step-name>.<field> — validated agent_output of prior step
  artifacts.<name>           — contents of artifacts/<name>.md
  change.<field>             — change metadata

Missing artifacts and missing steps render as empty (lenient).
Unknown top-level namespaces raise PromptRenderError (strict; catches typos).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import jinja2

from imp.engine.state import EventLog


class PromptRenderError(Exception):
    pass


class _EmptyOnMissing:
    def __getattr__(self, name: str) -> str:
        return ""

    def __getitem__(self, key: str) -> str:
        return ""

    def __str__(self) -> str:
        return ""


class _ArtifactsNamespace:
    def __init__(self, artifacts_dir: Path) -> None:
        self._dir = artifacts_dir

    def __getattr__(self, name: str) -> str:
        return self[name]

    def __getitem__(self, name: str) -> str:
        p = self._dir / f"{name}.md"
        return p.read_text() if p.exists() else ""


class _StepEvents:
    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload

    def __getattr__(self, name: str) -> Any:
        return self._payload.get(name, "")

    def __getitem__(self, name: str) -> Any:
        return self._payload.get(name, "")


class _EventsNamespace:
    def __init__(self, event_log: EventLog) -> None:
        self._outputs: dict[str, dict[str, Any]] = {}
        current: str | None = None
        for e in event_log.read():
            etype = e.get("type")
            if etype == "step_started":
                current = (e.get("payload") or {}).get("step")
            elif etype == "agent_output" and current:
                self._outputs[current] = e.get("payload") or {}

    def __getattr__(self, step: str) -> _StepEvents | _EmptyOnMissing:
        return self[step]

    def __getitem__(self, step: str) -> _StepEvents | _EmptyOnMissing:
        if step in self._outputs:
            return _StepEvents(self._outputs[step])
        return _EmptyOnMissing()


@dataclass(frozen=True, slots=True)
class _ChangeNamespace:
    id: int
    title: str


@dataclass(frozen=True, slots=True)
class PromptContext:
    change: _ChangeNamespace
    artifacts: _ArtifactsNamespace
    events: _EventsNamespace


def build_context(
    change_id: int, change_title: str, artifacts_dir: Path,
    event_log: EventLog,
) -> PromptContext:
    return PromptContext(
        change=_ChangeNamespace(id=change_id, title=change_title),
        artifacts=_ArtifactsNamespace(artifacts_dir),
        events=_EventsNamespace(event_log),
    )


_env = jinja2.Environment(
    undefined=jinja2.StrictUndefined,
    autoescape=False,
    keep_trailing_newline=True,
)


def render_prompt(template_source: str, ctx: PromptContext) -> str:
    """Render the template. Strict on top-level names; lenient inside namespaces."""
    try:
        return _env.from_string(template_source).render(
            change=ctx.change, artifacts=ctx.artifacts, events=ctx.events,
        )
    except jinja2.exceptions.UndefinedError as e:
        raise PromptRenderError(str(e)) from e
    except jinja2.exceptions.TemplateError as e:
        raise PromptRenderError(f"template error: {e}") from e
