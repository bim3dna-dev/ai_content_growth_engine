"""Machine-readable and Markdown YouTube performance reports."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from growth_engine.policy import Action, authorize
from growth_engine.storage import Workspace, stable_id, utc_now
from growth_engine.youtube.intelligence import FORMULA_VERSION, RULE_VERSION
from growth_engine.youtube.sync import validate_date_range


def _matching_analysis(
    workspace: Workspace,
    relative: str,
    start_date: str,
    end_date: str,
) -> dict[str, Any]:
    path = workspace.data_dir / relative
    if not path.is_file():
        raise ValueError(f"Required local artifact is missing: {relative}.")
    document = workspace.read_json(path)
    result = next(
        (
            item
            for item in document.get("analyses", [])
            if isinstance(item, dict)
            and item.get("start_date") == start_date
            and item.get("end_date") == end_date
        ),
        None,
    )
    if result is None:
        raise ValueError("Required local artifact does not match the exact date range.")
    return result


def generate_youtube_report(
    workspace: Workspace,
    alias: str,
    start_date: str,
    end_date: str,
    *,
    dry_run: bool = False,
    clock: Callable[[], str] = utc_now,
) -> dict[str, Any]:
    authorize(Action.LOCAL_ANALYSIS)
    validate_date_range(start_date, end_date)
    channel_path = (
        workspace.data_dir / "normalized" / "youtube" / alias / "channels.json"
    )
    if not channel_path.is_file():
        raise ValueError("No normalized channel snapshot exists.")
    channel = workspace.read_json(channel_path)
    videos_path = workspace.data_dir / "normalized" / "youtube" / alias / "videos.json"
    videos_document = (
        workspace.read_json(videos_path)
        if videos_path.is_file()
        else {"records": []}
    )
    videos = {
        str(item["video_id"]): item
        for item in videos_document.get("records", [])
        if isinstance(item, dict)
    }
    analysis = _matching_analysis(
        workspace,
        f"derived/youtube/{alias}/metrics.json",
        start_date,
        end_date,
    )
    diagnosis_set = _matching_analysis(
        workspace,
        f"derived/youtube/{alias}/diagnoses.json",
        start_date,
        end_date,
    )
    views = {
        str(metric["video_id"]): metric.get("value")
        for metric in analysis.get("metrics", [])
        if isinstance(metric, dict)
        and metric.get("name") == "views"
        and metric.get("video_id") is not None
    }
    def sort_value(video_id: str) -> tuple[float, str]:
        raw = views[video_id]
        numeric = float(raw) if isinstance(raw, (int, float)) else -1.0
        return -numeric, video_id

    ordered = sorted(views, key=sort_value)
    performance_rows = [
        {
            "video_id": video_id,
            "title": videos.get(video_id, {}).get("title"),
            "views": views[video_id],
        }
        for video_id in ordered
    ]
    diagnoses = [
        item for item in diagnosis_set.get("diagnoses", []) if isinstance(item, dict)
    ]
    warnings: list[str] = []
    unavailable = [
        item for item in videos.values() if item.get("availability") != "available"
    ]
    if unavailable:
        warnings.append(f"{len(unavailable)} uploaded videos were unavailable or deleted.")
    null_metrics = sum(
        1
        for metric in analysis.get("metrics", [])
        if isinstance(metric, dict) and metric.get("value") is None
    )
    if null_metrics:
        warnings.append(
            f"{null_metrics} derived metric values were null because source data was missing."
        )
    if not performance_rows:
        warnings.append("No video-level performance rows were available for the period.")
    experiments = list(
        dict.fromkeys(
            recommendation
            for diagnosis in diagnoses
            for recommendation in diagnosis.get("recommendations", [])
            if isinstance(recommendation, str)
        )
    )
    raw_root = workspace.data_dir / "raw" / "youtube" / alias
    raw_ids = [
        path.stem
        for path in sorted(raw_root.rglob("*.json"))
        if path.is_file()
    ] if raw_root.exists() else []
    generated_at = clock()
    report_id = stable_id(
        "youtube_report",
        {"channel": alias, "start_date": start_date, "end_date": end_date},
    )
    result = {
        "id": report_id,
        "schema_version": 1,
        "channel": alias,
        "start_date": start_date,
        "end_date": end_date,
        "state": "succeeded",
        "generated_at": generated_at,
        "network_requests": 0,
        "synchronization_provenance": {
            "raw_snapshot_ids": raw_ids,
            "analysis_id": analysis["id"],
            "diagnosis_id": diagnosis_set["id"],
        },
        "raw_api_facts": {
            "raw_snapshot_count": len(raw_ids),
            "provenance_note": "Raw responses remain in immutable snapshot files.",
        },
        "normalized_facts": {
            "channel": channel,
            "video_count": len(videos),
        },
        "calculated_metrics": {
            "period_summary": [
                metric
                for metric in analysis.get("metrics", [])
                if isinstance(metric, dict) and metric.get("video_id") is None
            ],
            "baselines": analysis.get("baselines", {}),
        },
        "top_performing_videos": performance_rows[:5],
        "underperforming_videos": list(reversed(performance_rows[-5:])),
        "diagnostic_observations": diagnoses,
        "data_quality_warnings": warnings,
        "recommendations": experiments,
        "formula_version": FORMULA_VERSION,
        "formulas": analysis.get("formulas", {}),
        "rule_version": RULE_VERSION,
    }
    markdown = _render_markdown(result)
    json_relative = f"reports/youtube/{alias}/{report_id}.json"
    markdown_relative = f"reports/youtube/{alias}/{report_id}.md"
    if not dry_run:
        workspace.write_json(json_relative, result)
        workspace.write_text(markdown_relative, markdown)
        workspace.audit(
            "youtube_report_created",
            {
                "channel": alias,
                "start_date": start_date,
                "end_date": end_date,
                "report_id": report_id,
                "network_requests": 0,
            },
        )
    return {
        **result,
        "json_path": str(workspace.data_dir / json_relative),
        "markdown_path": str(workspace.data_dir / markdown_relative),
        "markdown": markdown if dry_run else None,
    }


def _render_markdown(report: dict[str, Any]) -> str:
    channel = report["normalized_facts"]["channel"]
    lines = [
        f"# YouTube Performance Report — {report['channel']}",
        "",
        f"Period: {report['start_date']} to {report['end_date']}",
        "",
        "## 1. Synchronization provenance",
        "",
        f"- Raw snapshots: {len(report['synchronization_provenance']['raw_snapshot_ids'])}",
        f"- Analysis: `{report['synchronization_provenance']['analysis_id']}`",
        f"- Diagnosis: `{report['synchronization_provenance']['diagnosis_id']}`",
        "",
        "## 2. Channel snapshot",
        "",
        f"- Title: {channel.get('title') or 'Unavailable'}",
        f"- Channel ID: `{channel.get('channel_id')}`",
        f"- Public lifetime views: {channel.get('view_count')}",
        "",
        "## 3. Period summary",
        "",
    ]
    for metric in report["calculated_metrics"]["period_summary"]:
        lines.append(f"- {metric['name']}: {metric['value']} {metric['unit']}")
    lines.extend(["", "## 4. Top-performing videos", ""])
    for item in report["top_performing_videos"]:
        lines.append(f"- {item['title'] or item['video_id']}: {item['views']} views")
    lines.extend(["", "## 5. Underperforming videos", ""])
    for item in report["underperforming_videos"]:
        lines.append(f"- {item['title'] or item['video_id']}: {item['views']} views")
    lines.extend(["", "## 6. Content-level diagnoses", ""])
    for diagnosis in report["diagnostic_observations"]:
        lines.append(
            f"- `{diagnosis['video_id']}`: **{diagnosis['diagnosis']}** "
            f"({diagnosis['confidence']})"
        )
    lines.extend(["", "## 7. Data-quality and missing-data warnings", ""])
    lines.extend(f"- {warning}" for warning in report["data_quality_warnings"])
    lines.extend(["", "## 8. Recommended experiments", ""])
    lines.extend(f"- {item}" for item in report["recommendations"])
    lines.extend(
        [
            "",
            "## 9. Metric formulas and rule versions",
            "",
            f"- Formula version: `{report['formula_version']}`",
            f"- Diagnosis rules: `{report['rule_version']}`",
        ]
    )
    for name, formula in report["formulas"].items():
        lines.append(f"- {name}: `{formula}`")
    lines.extend(
        [
            "",
            "> Public Data API counters may be lifetime values. "
            "Period metrics come only from owner-authorized Analytics API rows.",
            "",
        ]
    )
    return "\n".join(lines)
