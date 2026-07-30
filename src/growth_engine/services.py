"""Deterministic Milestone 1 content intelligence workflows."""

from __future__ import annotations

from dataclasses import replace
from datetime import date
from typing import Any

from growth_engine.models import (
    Brief,
    DailyReport,
    Idea,
    IdeaSet,
    ResearchRecord,
    WorkflowState,
    require_transition,
)
from growth_engine.policy import Action, authorize
from growth_engine.storage import Workspace, stable_id, utc_now


def _artifact_from_receipt(workspace: Workspace, receipt: dict[str, Any]) -> dict[str, Any]:
    artifact = str(receipt["artifact"])
    relative = artifact.replace("\\", "/")
    return workspace.read_json(workspace.data_dir / relative)


def conduct_research(
    workspace: Workspace,
    topic: str,
    platforms: list[str],
    observations: list[str],
    *,
    dry_run: bool = False,
) -> dict[str, Any]:
    authorize(Action.LOCAL_RESEARCH)
    config = workspace.require_initialized()
    clean_topic = topic.strip() or str(config["niche"])
    clean_observations = [item.strip() for item in observations if item.strip()]
    if not clean_observations:
        clean_observations = [
            f"Validate the audience's most important questions about {clean_topic}.",
            f"Compare common explanations of {clean_topic} and identify missing practical detail.",
            "Prefer claims supported by creator expertise, primary evidence, "
            "or clearly cited sources.",
        ]
    clean_platforms = sorted(set(platforms))
    inputs = {
        "topic": clean_topic,
        "platforms": clean_platforms,
        "observations": clean_observations,
    }
    existing = workspace.receipt("research", inputs)
    if existing is not None:
        return _artifact_from_receipt(workspace, existing)
    record = ResearchRecord(
        id=stable_id("research", inputs),
        created_at=utc_now(),
        topic=clean_topic,
        platforms=clean_platforms,
        observations=clean_observations,
    )
    result = record.to_dict()
    if not dry_run:
        workspace.write_artifact("raw/research", record.id, result)
        workspace.write_receipt("research", inputs, f"raw/research/{record.id}.json")
        workspace.audit(
            "research_collected",
            {"artifact_id": record.id, "source": record.source, "platform_api_calls": 0},
        )
    return result


def _idea_templates(topic: str, observations: list[str]) -> list[tuple[str, str, str]]:
    evidence_hint = observations[0] if observations else f"Audience interest in {topic}"
    return [
        (
            f"{topic.title()}: What Actually Matters",
            "Separate high-impact fundamentals from distracting conventional wisdom.",
            f"Help the audience make one confident decision; starting evidence: {evidence_hint}",
        ),
        (
            f"3 Mistakes People Make With {topic.title()}",
            "Teach through realistic mistakes without shaming beginners.",
            "Prevent wasted time and give viewers a practical correction for each mistake.",
        ),
        (
            f"A Practical Beginner's Guide to {topic.title()}",
            "Build a clear first-week path with concrete checkpoints.",
            "Replace information overload with a small, achievable sequence.",
        ),
        (
            f"I Tested the Common Advice About {topic.title()}",
            "Compare popular claims against transparent criteria and evidence.",
            "Give the audience a trustworthy way to evaluate advice for themselves.",
        ),
        (
            f"Before You Invest in {topic.title()}, Ask These Questions",
            "Frame the topic as a decision guide rather than a sales pitch.",
            "Help the audience avoid poor-fit purchases and clarify priorities.",
        ),
        (
            f"The Hidden Trade-offs in {topic.title()}",
            "Explain what changes when optimizing for cost, speed, quality, or simplicity.",
            "Enable nuanced choices instead of promising a universal best answer.",
        ),
        (
            f"{topic.title()} Explained With One Real Example",
            "Use a grounded scenario to connect theory to action.",
            "Make an abstract topic memorable and immediately applicable.",
        ),
        (
            f"A Better Workflow for {topic.title()}",
            "Show a repeatable process and explain why each step exists.",
            "Give experienced viewers a system they can adapt, not copy blindly.",
        ),
    ]


def generate_ideas(
    workspace: Workspace,
    count: int,
    research_id: str | None,
    *,
    dry_run: bool = False,
) -> dict[str, Any]:
    authorize(Action.LOCAL_ANALYSIS)
    workspace.require_initialized()
    research_records = workspace.artifacts("raw/research")
    selected = next((item for item in research_records if item.get("id") == research_id), None)
    if selected is None and research_id is not None:
        raise ValueError(f"Research artifact '{research_id}' was not found.")
    if selected is None:
        selected = workspace.latest("raw/research")
    if selected is None:
        raise ValueError("No research exists. Run 'growth-engine research' first.")
    require_transition(WorkflowState(str(selected["state"])), WorkflowState.GENERATED)
    count = max(1, min(count, 20))
    inputs = {"research_id": selected["id"], "count": count}
    existing = workspace.receipt("ideas_generate", inputs)
    if existing is not None:
        return _artifact_from_receipt(workspace, existing)
    topic = str(selected["topic"])
    observations = [str(item) for item in selected.get("observations", [])]
    platforms = [str(item) for item in selected.get("platforms", [])]
    templates = _idea_templates(topic, observations)
    ideas: list[Idea] = []
    for index in range(count):
        title, angle, value = templates[index % len(templates)]
        cycle = index // len(templates)
        if cycle:
            title = f"{title} — Part {cycle + 1}"
        idea_inputs = {"research_id": selected["id"], "index": index, "title": title}
        ideas.append(
            Idea(
                id=stable_id("idea", idea_inputs),
                title=title,
                angle=angle,
                audience_value=value,
                platforms=platforms,
                research_id=str(selected["id"]),
                effort=2 + (index * 3) % 4,
                originality=3 + (index * 2) % 3,
                relevance=5 - (index % 3),
            )
        )
    idea_set = IdeaSet(
        id=stable_id("ideas", inputs),
        created_at=utc_now(),
        research_id=str(selected["id"]),
        topic=topic,
        ideas=ideas,
    )
    result = idea_set.to_dict()
    if not dry_run:
        workspace.write_artifact("derived/ideas", idea_set.id, result)
        workspace.write_receipt("ideas_generate", inputs, f"derived/ideas/{idea_set.id}.json")
        workspace.audit(
            "ideas_generated",
            {"artifact_id": idea_set.id, "count": count, "ai_provider": "deterministic_local"},
        )
    return result


def rank_ideas(
    workspace: Workspace, idea_set_id: str | None, *, dry_run: bool = False
) -> dict[str, Any]:
    authorize(Action.LOCAL_ANALYSIS)
    workspace.require_initialized()
    sets = workspace.artifacts("derived/ideas")
    selected = next((item for item in sets if item.get("id") == idea_set_id), None)
    if selected is None and idea_set_id is not None:
        raise ValueError(f"Idea set '{idea_set_id}' was not found.")
    if selected is None:
        generated_sets = [
            item for item in sets if item.get("state") == WorkflowState.GENERATED
        ]
        selected = max(
            generated_sets,
            key=lambda item: str(item.get("created_at", "")),
            default=None,
        )
    if selected is None:
        raise ValueError("No ideas exist. Run 'growth-engine ideas generate' first.")
    require_transition(WorkflowState(str(selected["state"])), WorkflowState.RANKED)
    inputs = {"idea_set_id": selected["id"], "algorithm": "value_v1"}
    existing = workspace.receipt("ideas_rank", inputs)
    if existing is not None:
        return _artifact_from_receipt(workspace, existing)
    source_ideas = selected.get("ideas", [])
    if not isinstance(source_ideas, list):
        raise ValueError("Idea artifact is invalid: 'ideas' must be a list.")
    scored: list[Idea] = []
    for raw in source_ideas:
        if not isinstance(raw, dict):
            continue
        effort = int(raw["effort"])
        originality = int(raw["originality"])
        relevance = int(raw["relevance"])
        score = round(relevance * 0.5 + originality * 0.35 + (6 - effort) * 0.15, 2)
        scored.append(
            Idea(
                id=str(raw["id"]),
                title=str(raw["title"]),
                angle=str(raw["angle"]),
                audience_value=str(raw["audience_value"]),
                platforms=[str(item) for item in raw["platforms"]],
                research_id=str(raw["research_id"]),
                effort=effort,
                originality=originality,
                relevance=relevance,
                score=score,
            )
        )
    scored.sort(key=lambda idea: (-(idea.score or 0), idea.effort, idea.id))
    ranked = [replace(idea, rank=index + 1) for index, idea in enumerate(scored)]
    ranked_id = stable_id("ranking", inputs)
    result = IdeaSet(
        id=ranked_id,
        created_at=utc_now(),
        research_id=str(selected["research_id"]),
        topic=str(selected["topic"]),
        ideas=ranked,
        state=WorkflowState.RANKED,
    ).to_dict()
    if not dry_run:
        workspace.write_artifact("derived/ideas", ranked_id, result)
        workspace.write_receipt("ideas_rank", inputs, f"derived/ideas/{ranked_id}.json")
        workspace.audit("ideas_ranked", {"artifact_id": ranked_id, "algorithm": "value_v1"})
    return result


def create_brief(
    workspace: Workspace, idea_id: str | None, *, dry_run: bool = False
) -> dict[str, Any]:
    authorize(Action.LOCAL_ANALYSIS)
    workspace.require_initialized()
    idea_sets = workspace.artifacts("derived/ideas")
    candidates: list[dict[str, Any]] = []
    for idea_set in idea_sets:
        if idea_set.get("state") != WorkflowState.RANKED:
            continue
        for raw in idea_set.get("ideas", []):
            if isinstance(raw, dict):
                candidates.append(raw)
    selected = next((item for item in candidates if item.get("id") == idea_id), None)
    if selected is None and idea_id is not None:
        raise ValueError(f"Idea '{idea_id}' was not found.")
    if selected is None:
        ranked = [item for item in candidates if item.get("rank") is not None]
        if not ranked:
            raise ValueError("No ranked ideas exist. Run 'growth-engine ideas rank' first.")
        selected = min(ranked, key=lambda item: int(item["rank"]))
    research_id = str(selected["research_id"])
    research = next(
        (
            item
            for item in workspace.artifacts("raw/research")
            if item.get("id") == research_id
        ),
        None,
    )
    evidence = [str(item) for item in (research or {}).get("observations", [])]
    inputs = {"idea_id": selected["id"], "template": "brief_v1"}
    existing = workspace.receipt("brief_create", inputs)
    if existing is not None:
        return _artifact_from_receipt(workspace, existing)
    title = str(selected["title"])
    platforms = [str(item) for item in selected["platforms"]]
    adaptations = {
        platform: {
            "youtube": "Develop the full argument with chapters, examples, and source notes.",
            "instagram": "Use a concise carousel or Reel with one takeaway per beat.",
            "tiktok": "Lead with the practical tension, demonstrate quickly, and avoid filler.",
        }[platform]
        for platform in platforms
    }
    brief = Brief(
        id=stable_id("brief", inputs),
        created_at=utc_now(),
        idea_id=str(selected["id"]),
        title=title,
        objective="Deliver a useful, original explanation that earns attention through relevance.",
        audience="People actively seeking a practical and trustworthy path through this topic.",
        promise=str(selected["audience_value"]),
        hook=f"Most advice about this misses the decision that matters first: {title}.",
        outline=[
            "Open with the audience problem and state the concrete promise.",
            f"Establish the point of view: {selected['angle']}",
            "Walk through evidence, a real example, and important trade-offs.",
            "Summarize an action the viewer can take without buying or following.",
            "Invite a genuine question for future research; do not solicit artificial engagement.",
        ],
        platform_adaptations=adaptations,
        evidence=evidence,
    )
    result = brief.to_dict()
    if not dry_run:
        workspace.write_artifact("derived/briefs", brief.id, result)
        workspace.write_receipt("brief_create", inputs, f"derived/briefs/{brief.id}.json")
        workspace.audit(
            "brief_created",
            {"artifact_id": brief.id, "externally_visible": False, "approval_required": False},
        )
    return result


def create_daily_report(
    workspace: Workspace, report_date: date, *, dry_run: bool = False
) -> dict[str, Any]:
    authorize(Action.LOCAL_ANALYSIS)
    workspace.require_initialized()
    inputs = {"date": report_date.isoformat(), "template": "daily_v1"}
    existing = workspace.receipt("report_daily", inputs)
    if existing is not None:
        return _artifact_from_receipt(workspace, existing)
    research = workspace.artifacts("raw/research")
    idea_sets = workspace.artifacts("derived/ideas")
    briefs = workspace.artifacts("derived/briefs")
    raw_metrics = [
        metric
        for metric in workspace.artifacts("raw/metrics")
        if str(metric.get("date", "")) == report_date.isoformat()
    ]
    ranked_ideas = sum(
        1
        for item in idea_sets
        if item.get("state") == WorkflowState.RANKED
        for idea in item.get("ideas", [])
        if isinstance(idea, dict)
    )
    recommendations = []
    if not research:
        recommendations.append("Collect one audience-centered research topic.")
    if research and not idea_sets:
        recommendations.append("Generate ideas from the latest research.")
    if idea_sets and not ranked_ideas:
        recommendations.append("Rank ideas using the transparent value-first rubric.")
    if ranked_ideas and not briefs:
        recommendations.append("Create a brief for the highest-ranked idea.")
    if not raw_metrics:
        recommendations.append(
            "No raw platform metrics were imported; avoid performance conclusions "
            "until evidence exists."
        )
    report = DailyReport(
        id=stable_id("report", inputs),
        created_at=utc_now(),
        report_date=report_date.isoformat(),
        raw_metrics=raw_metrics,
        recommendations=recommendations,
        pipeline={
            "research_records": len(research),
            "idea_sets": len(idea_sets),
            "ranked_ideas": ranked_ideas,
            "briefs": len(briefs),
        },
        notes=[
            "Raw metrics are stored separately under raw/metrics.",
            "Recommendations are derived locally and do not trigger platform actions.",
        ],
    )
    result = report.to_dict()
    if not dry_run:
        workspace.write_artifact("reports", report.id, result)
        workspace.write_receipt("report_daily", inputs, f"reports/{report.id}.json")
        workspace.audit(
            "daily_report_created",
            {"artifact_id": report.id, "raw_metric_records": len(raw_metrics)},
        )
    return result
