"""Shared data model for the eval framework. Every dataclass that crosses a
module boundary is defined here ONCE so types stay consistent across the
runner, grader, mining, and scorecard. All types are frozen. The PERSISTED types
— `Fixture`, `Variant`, `RunResult`, `Telemetry` — carry `to_dict`/`from_dict`
(fixtures are stored as data; `RunResult` is cached as JSON). `GradeResult`,
`CellResult`, and `FindingMatch` are in-memory analysis types (recomputed from
cached `RunResult`s), so they intentionally have no serialization."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

SEVERITIES = ("Critical", "Major", "Important", "Minor", "Nit")
BLOCKING_SEVERITIES = ("Critical", "Major")  # spec "Decision rules": blocking recall
# v1 ships monolithic + slice-panel (+ completeness slice as a panel member).
# "ensemble" (the SAME reviewer run N times under different models) is DEFERRED:
# role_to_model_map is keyed by agent_name, so same-agent x N is inexpressible.
# Adding it = switch role_to_model_map to a `members: tuple[Member, ...]` list
# (Member = agent_name+provider+model+override). Not needed for Q1-Q4.
STRUCTURES = ("monolithic", "slice-panel")


@dataclass(frozen=True)
class ExpectedFinding:
    """One labeled bug a good review SHOULD catch. `locus` is pinned to the
    post-diff (head) tree; `symbol` is the enclosing symbol name so a match
    survives line drift (spec Grader §, Matching semantics)."""

    locus: str  # "file.py:line" in head line-space
    symbol: str  # enclosing symbol name; match is symbol-first, line-as-hint
    description: str
    severity: str  # one of SEVERITIES
    must_catch: bool

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> ExpectedFinding:
        return cls(
            locus=d["locus"],
            symbol=d["symbol"],
            description=d["description"],
            severity=d["severity"],
            must_catch=bool(d["must_catch"]),
        )


@dataclass(frozen=True)
class CleanLocus:
    """A place a good review should NOT flag. The precision anchor."""

    locus: str
    symbol: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> CleanLocus:
        return cls(locus=d["locus"], symbol=d["symbol"])


@dataclass(frozen=True)
class FixtureMeta:
    reviewer_type: str | None
    original_model: str | None
    blocking: bool | None
    was_fixed: bool | None
    stratum: str | None = None        # "in-diff" | "cross-cutting" | "deep-tail" | "clean"
    independence: str | None = None   # "experiment-seed" | "independent-history" | "blind-authored"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> FixtureMeta:
        return cls(
            reviewer_type=d.get("reviewer_type"),
            original_model=d.get("original_model"),
            blocking=d.get("blocking"),
            was_fixed=d.get("was_fixed"),
            stratum=d.get("stratum"),
            independence=d.get("independence"),
        )


@dataclass(frozen=True)
class Fixture:
    """A graded scenario. Stored as data (directory-per-fixture); does NOT depend
    on the harness or grader."""

    id: str
    source: str  # "mined" | "curated"
    base_sha: str
    head_sha: str
    diff: str
    task_context: str
    expected_findings: tuple[ExpectedFinding, ...]
    clean_loci: tuple[CleanLocus, ...]
    meta: FixtureMeta

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "source": self.source,
            "base_sha": self.base_sha,
            "head_sha": self.head_sha,
            "diff": self.diff,
            "task_context": self.task_context,
            "expected_findings": [f.to_dict() for f in self.expected_findings],
            "clean_loci": [c.to_dict() for c in self.clean_loci],
            "meta": self.meta.to_dict(),
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Fixture:
        return cls(
            id=d["id"],
            source=d["source"],
            base_sha=d["base_sha"],
            head_sha=d["head_sha"],
            diff=d["diff"],
            task_context=d["task_context"],
            expected_findings=tuple(
                ExpectedFinding.from_dict(f) for f in d.get("expected_findings", [])
            ),
            clean_loci=tuple(CleanLocus.from_dict(c) for c in d.get("clean_loci", [])),
            meta=FixtureMeta.from_dict(d.get("meta", {})),
        )


@dataclass(frozen=True)
class Variant:
    """The thing under test. A prompt edit or a model swap is a new Variant.
    `role_to_model_map` binds agent_name -> (provider_name, model_name) AT THE
    SINGLE-AGENT SEAM, bypassing tier/role inheritance (spec Variant §)."""

    id: str
    role_to_model_map: dict[str, tuple[str, str]] = field(hash=False)
    prompt_overrides: dict[str, str] = field(default_factory=dict, hash=False)  # agent_name -> path
    structure: str = "monolithic"
    n_repeats: int = 1

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "role_to_model_map": {k: list(v) for k, v in self.role_to_model_map.items()},
            "prompt_overrides": dict(self.prompt_overrides),
            "structure": self.structure,
            "n_repeats": self.n_repeats,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Variant:
        return cls(
            id=d["id"],
            role_to_model_map={k: tuple(v) for k, v in d["role_to_model_map"].items()},
            prompt_overrides=dict(d.get("prompt_overrides", {})),
            structure=d.get("structure", "monolithic"),
            n_repeats=int(d.get("n_repeats", 1)),
        )


@dataclass(frozen=True)
class ReviewFinding:
    """One finding parsed out of a produced review (used for precision + panel
    dedup). `blocking` = severity in BLOCKING_SEVERITIES OR an explicit blocking
    verdict from the reviewer's output schema."""

    symbol: str
    locus: str
    description: str
    severity: str
    blocking: bool


@dataclass(frozen=True)
class Telemetry:
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_creation_tokens: int
    tool_calls: int
    wall_seconds: float

    def __add__(self, other: Telemetry) -> Telemetry:
        return Telemetry(
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            cache_read_tokens=self.cache_read_tokens + other.cache_read_tokens,
            cache_creation_tokens=self.cache_creation_tokens + other.cache_creation_tokens,
            tool_calls=self.tool_calls + other.tool_calls,
            wall_seconds=self.wall_seconds + other.wall_seconds,
        )

    @classmethod
    def zero(cls) -> Telemetry:
        return cls(0, 0, 0, 0, 0, 0.0)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Telemetry:
        return cls(
            input_tokens=int(d["input_tokens"]),
            output_tokens=int(d["output_tokens"]),
            cache_read_tokens=int(d["cache_read_tokens"]),
            cache_creation_tokens=int(d["cache_creation_tokens"]),
            tool_calls=int(d["tool_calls"]),
            wall_seconds=float(d["wall_seconds"]),
        )


@dataclass(frozen=True)
class RunResult:
    """The output of running one Variant against one Fixture once."""

    variant_id: str
    fixture_id: str
    repeat_index: int
    raw_review: str  # the reviewer's full `findings` prose (union prose for panels)
    findings: tuple[ReviewFinding, ...]
    telemetry: Telemetry
    dollars: float = 0.0
    imp_commit_sha: str = ""  # the imp HEAD SHA the cell was produced at (P1)

    def to_dict(self) -> dict[str, Any]:
        return {
            "variant_id": self.variant_id,
            "fixture_id": self.fixture_id,
            "repeat_index": self.repeat_index,
            "raw_review": self.raw_review,
            "findings": [asdict(f) for f in self.findings],
            "telemetry": self.telemetry.to_dict(),
            "dollars": self.dollars,
            "imp_commit_sha": self.imp_commit_sha,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> RunResult:
        return cls(
            variant_id=d["variant_id"],
            fixture_id=d["fixture_id"],
            repeat_index=int(d["repeat_index"]),
            raw_review=d["raw_review"],
            findings=tuple(
                ReviewFinding(
                    symbol=f["symbol"],
                    locus=f["locus"],
                    description=f["description"],
                    severity=f["severity"],
                    blocking=bool(f["blocking"]),
                )
                for f in d.get("findings", [])
            ),
            telemetry=Telemetry.from_dict(d["telemetry"]),
            dollars=float(d.get("dollars", 0.0)),
            imp_commit_sha=d.get("imp_commit_sha", ""),
        )


@dataclass(frozen=True)
class FindingMatch:
    """Whether one ExpectedFinding was caught by a review, and how well."""

    expected: ExpectedFinding
    match_kind: str  # "exact" | "partial" | "none"
    review_finding: ReviewFinding | None

    @property
    def caught(self) -> bool:
        return self.match_kind in ("exact", "partial")


@dataclass(frozen=True)
class GradeResult:
    """A graded (variant, fixture, repeat). Recall + precision per the matching
    contract. `false_positives` = curated flags outside any known bug locus;
    `inspect_flags` = mined merged-clean hits (directional only, NOT counted as
    false positives — spec Grader §)."""

    variant_id: str
    fixture_id: str
    repeat_index: int
    matches: tuple[FindingMatch, ...]
    false_positives: tuple[ReviewFinding, ...]
    inspect_flags: tuple[ReviewFinding, ...]
    # Review findings that DID hit a known bug locus — the precision numerator, in
    # the SAME unit (ReviewFinding) as false_positives, so precision = tp/(tp+fp)
    # is a real ratio (not caught-expected / review-flags, two different
    # populations). Defaulted so existing positional constructions stay valid.
    true_positive_flags: tuple[ReviewFinding, ...] = ()

    def _recall(self, *, blocking_only: bool) -> float | None:
        rel = [
            m
            for m in self.matches
            if m.expected.must_catch
            and (not blocking_only or m.expected.severity in BLOCKING_SEVERITIES)
        ]
        if not rel:
            return None
        return sum(1 for m in rel if m.caught) / len(rel)

    @property
    def blocking_recall(self) -> float | None:
        return self._recall(blocking_only=True)

    @property
    def nit_recall(self) -> float | None:
        rel = [
            m
            for m in self.matches
            if m.expected.must_catch and m.expected.severity not in BLOCKING_SEVERITIES
        ]
        if not rel:
            return None
        return sum(1 for m in rel if m.caught) / len(rel)


@dataclass(frozen=True)
class CellResult:
    """One aggregated (variant, fixture) cell over n_repeats. CI fields filled by
    aggregate.py."""

    variant_id: str
    fixture_id: str
    n: int
    blocking_recall_mean: float | None
    blocking_recall_ci: tuple[float, float] | None
    precision_mean: float | None
    precision_ci: tuple[float, float] | None
    dollars_mean: float
    low_n: bool
    # Reported SEPARATELY from blocking recall (spec: a nit-driven aggregate must
    # not decide droppability). Informational only — no verdict uses it.
    nit_recall_mean: float | None = None
