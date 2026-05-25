"""Compile parent AgentDef's subagent references into deepagent subagent specs.

V0: subagents share the parent's model. The exact dict shape varies by
deepagents version; Task 17's runner passes them through to create_deep_agent.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from langchain_core.language_models import BaseChatModel

from imp.agents.output_schema import schema_to_pydantic_model
from imp.engine.loader import parse_agent_file
from imp.engine.types import AgentDef


def compile_subagent_specs(
    parent: AgentDef, agents_dir: Path, model: BaseChatModel,
) -> list[Any]:
    out: list[Any] = []
    for child_name in parent.subagents:
        child_path = agents_dir / f"{child_name}.md"
        if not child_path.exists():
            raise FileNotFoundError(
                f"subagent {child_name!r} referenced by {parent.name!r} "
                f"not found at {child_path}"
            )
        child = parse_agent_file(child_path)
        out.append({
            "name": child.name,
            "description": child.description or f"subagent {child.name}",
            "prompt": child.prompt_template_source,
            "model": model,
            "response_format": schema_to_pydantic_model(child.output_schema),
        })
    return out
