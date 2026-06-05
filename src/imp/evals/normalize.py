"""LLM-normalize free reviewer prose into structured ExpectedFindings (mining
step 3 / spec). Regex alone is insufficient — prose has no schema. A normalized
finding's `must_catch` is set from the reviewer's blocking signal; mining (Task
18) further gates must_catch on the blocking->fixed rework signal."""

from __future__ import annotations

import json
from importlib.resources import files
from pathlib import Path

from imp.engine.loader import parse_agent_file
from imp.evals.driver import drive_agent_sync
from imp.evals.models import ExpectedFinding


def _normalizer_path() -> Path:
    return Path(str(files("imp"))) / "evals" / "data" / "agents" / "label-normalizer.md"


def normalize_findings(
    prose: str,
    *,
    project_root: Path,
    provider: str,
    model: str,
) -> tuple[ExpectedFinding, ...]:
    agent_def = parse_agent_file(_normalizer_path())
    output, _telem = drive_agent_sync(
        agent_def=agent_def,
        provider_name=provider,
        model_name=model,
        project_root=project_root,
        opening_message=prose,
        agent_id="eval:normalizer",
    )
    if not output:
        return ()
    try:
        items = json.loads(output.get("findings_json", "[]"))
    except (json.JSONDecodeError, TypeError):
        return ()
    out: list[ExpectedFinding] = []
    for it in items:
        out.append(
            ExpectedFinding(
                locus=str(it.get("locus", "")),
                symbol=str(it.get("symbol", "")),
                description=str(it.get("description", "")),
                severity=str(it.get("severity", "Minor")),
                must_catch=bool(it.get("blocking", False)),
            )
        )
    return tuple(out)
