"""Prompt template rendering for agent .md bodies.

Six namespaces (spec §7.5):
  change.id — stable identity; change.title — human title
  events.<step-name>.<field> — validated agent_output of prior step;
      missing steps render as empty (lenient-within)
  vars.<name> — workflow vars from state.vars; a typo'd key raises at render
      because the dict is passed directly under StrictUndefined (strict-within)
  project.<fact> — project facts; missing key raises (strict-within)
  run.autonomous — whether the run may auto-take recommendations at
      recommendation-bearing gates (bool); a missing attr under run raises
      (strict-within), like change/project
  step.lap — this step's 1-based lap number (1 = first forward pass);
      step.prior_laps — retained {lap, outcome, head} entries of prior laps

Unknown top-level namespaces raise PromptRenderError (strict; catches typos).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

import jinja2

from imp.engine.state import EventLog, State

if TYPE_CHECKING:
    from imp.engine.change import Change


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
    id: str
    title: str
    home: str = ""
    cur_base_head: str = ""
    orig_base_head: str = ""


@dataclass(frozen=True, slots=True)
class _RunNamespace:
    autonomous: bool = False
    opening: str = ""


@dataclass(frozen=True, slots=True)
class _StepNamespace:
    lap: int
    prior_laps: tuple[dict[str, Any], ...]


class _ProjectNamespace:
    """Strict project-facts namespace: raises UndefinedError on a missing key,
    AttributeError for dunder/leading-underscore names."""

    def __init__(self, facts: dict[str, str]) -> None:
        self._facts = facts

    def __getattr__(self, name: str) -> str:
        if name.startswith("_"):
            raise AttributeError(name)
        try:
            return self._facts[name]
        except KeyError:
            raise jinja2.exceptions.UndefinedError(
                f"project has no fact {name!r}"
            ) from None

    def __getitem__(self, key: str) -> str:
        try:
            return self._facts[key]
        except KeyError:
            raise jinja2.exceptions.UndefinedError(
                f"project has no fact {key!r}"
            ) from None


@dataclass(frozen=True, slots=True)
class PromptContext:
    change: _ChangeNamespace
    events: _EventsNamespace
    vars: dict[str, Any]
    project: _ProjectNamespace
    run: _RunNamespace
    step: _StepNamespace


def build_context(
    change_title: str, event_log: EventLog,
    *, quest_id: str = "", quest_home: str = "",
    cur_base_head: str = "", orig_base_head: str = "",
    vars: dict[str, Any] | None = None,
    project_facts: dict[str, str] | None = None,
    autonomous: bool = False, opening: str = "",
    prior_laps: list[dict[str, Any]] | None = None,
) -> PromptContext:
    facts = dict(project_facts or {})
    laps = tuple(prior_laps or ())
    # lap from the stored lap, not len: history truncates to OUTCOME_HISTORY_K so len saturates.
    return PromptContext(
        change=_ChangeNamespace(id=quest_id, title=change_title,
                                home=quest_home, cur_base_head=cur_base_head,
                                orig_base_head=orig_base_head),
        events=_EventsNamespace(event_log),
        vars=dict(vars or {}),
        project=_ProjectNamespace(facts),
        run=_RunNamespace(autonomous=autonomous, opening=opening),
        step=_StepNamespace(
            lap=(laps[-1]["lap"] + 1 if laps else 1), prior_laps=laps),
    )


def render_label(
    label: str | None, change: Change | None, log: EventLog | None,
    state: State | None = None,
) -> str | None:
    if label is None or "{{" not in label:
        return label
    if change is None or log is None:
        # No change/log context (the State.step_label seed path): render with no
        # template context. A label that needs vars/change raises UndefinedError
        # under StrictUndefined and falls back to the raw template text.
        try:
            return _env_for(None).from_string(label).render()
        except jinja2.exceptions.TemplateError:
            return label
    try:
        return render_prompt(label, build_context(
            change_title=change.title, event_log=log,
            quest_id=change.id, quest_home=f".imp/changes/{change.id}",
            cur_base_head=change.cur_base_head,
            orig_base_head=change.orig_base_head,
            vars=state.vars if state is not None else {},
        ))
    except PromptRenderError:
        return label


_ENV_KWARGS: dict[str, Any] = dict(
    undefined=jinja2.StrictUndefined,
    autoescape=False,
    keep_trailing_newline=True,
    trim_blocks=True,
    lstrip_blocks=True,
)


_env = jinja2.Environment(**_ENV_KWARGS)


def _env_for(workflow_dir: Path | None) -> jinja2.Environment:
    # FileSystemLoader lets {% include %} resolve partials; loader-less env is the default.
    if workflow_dir is None:
        return _env
    return jinja2.Environment(
        loader=jinja2.FileSystemLoader(str(workflow_dir)), **_ENV_KWARGS)


def render_prompt(
    template_source: str, ctx: PromptContext, *, workflow_dir: Path | None = None,
) -> str:
    """Render the template. Strict on top-level names; lenient inside namespaces.
    Pass workflow_dir to resolve `{% include %}` of shared partials."""
    try:
        return _env_for(workflow_dir).from_string(template_source).render(
            change=ctx.change, events=ctx.events, vars=ctx.vars,
            project=ctx.project, run=ctx.run, step=ctx.step,
        )
    except jinja2.exceptions.UndefinedError as e:
        raise PromptRenderError(str(e)) from e
    except jinja2.exceptions.TemplateError as e:
        raise PromptRenderError(f"template error: {e}") from e
    except TypeError as e:
        # Jinja2 raises TypeError (not TemplateError) for includes without a loader.
        if "no loader" not in str(e):
            raise
        raise PromptRenderError(f"template error: {e}") from e
