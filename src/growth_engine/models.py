"""Typed domain models and explicit workflow transitions."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import StrEnum
from typing import Any


class Platform(StrEnum):
    YOUTUBE = "youtube"
    INSTAGRAM = "instagram"
    TIKTOK = "tiktok"


class WorkflowState(StrEnum):
    COLLECTED = "collected"
    GENERATED = "generated"
    RANKED = "ranked"
    BRIEFED = "briefed"
    REPORTED = "reported"


ALLOWED_TRANSITIONS: dict[WorkflowState, frozenset[WorkflowState]] = {
    WorkflowState.COLLECTED: frozenset({WorkflowState.GENERATED}),
    WorkflowState.GENERATED: frozenset({WorkflowState.RANKED}),
    WorkflowState.RANKED: frozenset({WorkflowState.BRIEFED}),
    WorkflowState.BRIEFED: frozenset({WorkflowState.REPORTED}),
    WorkflowState.REPORTED: frozenset(),
}


def require_transition(current: WorkflowState, target: WorkflowState) -> None:
    """Reject an invalid workflow state transition."""
    if target not in ALLOWED_TRANSITIONS[current]:
        msg = f"Invalid workflow transition: {current.value} -> {target.value}"
        raise ValueError(msg)


@dataclass(frozen=True, slots=True)
class ResearchRecord:
    id: str
    created_at: str
    topic: str
    platforms: list[str]
    observations: list[str]
    source: str = "creator_input"
    state: str = WorkflowState.COLLECTED
    schema_version: int = 1

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class Idea:
    id: str
    title: str
    angle: str
    audience_value: str
    platforms: list[str]
    research_id: str
    effort: int
    originality: int
    relevance: int
    score: float | None = None
    rank: int | None = None


@dataclass(frozen=True, slots=True)
class IdeaSet:
    id: str
    created_at: str
    research_id: str
    topic: str
    ideas: list[Idea]
    state: str = WorkflowState.GENERATED
    schema_version: int = 1

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class Brief:
    id: str
    created_at: str
    idea_id: str
    title: str
    objective: str
    audience: str
    promise: str
    hook: str
    outline: list[str]
    platform_adaptations: dict[str, str]
    evidence: list[str]
    approval_status: str = "not_required_local_draft"
    state: str = WorkflowState.BRIEFED
    schema_version: int = 1

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class DailyReport:
    id: str
    created_at: str
    report_date: str
    raw_metrics: list[dict[str, Any]]
    recommendations: list[str]
    pipeline: dict[str, int]
    notes: list[str] = field(default_factory=list)
    state: str = WorkflowState.REPORTED
    schema_version: int = 1

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
