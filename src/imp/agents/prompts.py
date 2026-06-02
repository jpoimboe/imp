"""Prompt template rendering for agent .md bodies.

Three namespaces (spec §7.5):
  change.slug / change.title — change identity
  events.<step-name>.<field> — validated agent_output of prior step;
      missing steps render as empty (lenient-within)
  vars.<name> — workflow vars from state.vars; a typo'd key raises at render
      because the dict is passed directly under StrictUndefined (strict-within)

Unknown top-level namespaces raise PromptRenderError (strict; catches typos).
"""

from __future__ import annotations

from dataclasses import dataclass
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
    slug: str
    title: str
    auto_titled: bool = False


@dataclass(frozen=True, slots=True)
class PromptContext:
    change: _ChangeNamespace
    events: _EventsNamespace
    vars: dict[str, Any]


def build_context(
    change_slug: str, change_title: str,
    event_log: EventLog,
    *, change_auto_titled: bool = False,
    vars: dict[str, Any] | None = None,
) -> PromptContext:
    return PromptContext(
        change=_ChangeNamespace(
            slug=change_slug, title=change_title, auto_titled=change_auto_titled,
        ),
        events=_EventsNamespace(event_log),
        vars=dict(vars or {}),
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
            change=ctx.change, events=ctx.events, vars=ctx.vars,
        )
    except jinja2.exceptions.UndefinedError as e:
        raise PromptRenderError(str(e)) from e
    except jinja2.exceptions.TemplateError as e:
        raise PromptRenderError(f"template error: {e}") from e
