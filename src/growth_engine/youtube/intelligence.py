"""Deterministic YouTube performance formulas and diagnosis rules."""

from __future__ import annotations

import math
import statistics
from collections.abc import Callable
from datetime import date, datetime
from typing import Any

from growth_engine.policy import Action, authorize
from growth_engine.storage import Workspace, stable_id, utc_now
from growth_engine.youtube.sync import validate_date_range

FORMULA_VERSION = "youtube-formulas-v1"
RULE_VERSION = "youtube-diagnosis-v1"
FALLBACK_THRESHOLDS = {
    "impressions": 1000.0,
    "impression_click_through_rate": 2.0,
    "average_percentage_viewed": 40.0,
    "subscriber_conversion_rate": 0.5,
    "engagement_rate": 3.0,
}

_SOURCE_NAMES = {
    "views": "views",
    "estimated_minutes_watched": "estimatedMinutesWatched",
    "average_view_duration": "averageViewDuration",
    "average_percentage_viewed": "averageViewPercentage",
    "subscribers_gained": "subscribersGained",
    "subscribers_lost": "subscribersLost",
    "likes": "likes",
    "comments": "comments",
    "shares": "shares",
    "impressions": "videoThumbnailImpressions",
    "impression_click_through_rate": "videoThumbnailImpressionsClickRate",
}


def _number(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)) and math.isfinite(float(value)):
        return float(value)
    return None


def safe_rate(
    numerator: float | None, denominator: float | None
) -> tuple[float | None, str | None]:
    if numerator is None:
        return None, "missing_numerator"
    if denominator is None:
        return None, "missing_denominator"
    if denominator == 0:
        return None, "zero_denominator"
    return round(numerator / denominator * 100, 4), None


def _percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * fraction
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def _metric_record(
    *,
    video_id: str | None,
    name: str,
    value: float | None,
    unit: str,
    formula_id: str,
    source_ids: list[str],
    start_date: str,
    end_date: str,
    calculated_at: str,
    nullability_reason: str | None = None,
) -> dict[str, Any]:
    identity = {
        "video_id": video_id,
        "name": name,
        "start_date": start_date,
        "end_date": end_date,
        "formula_version": FORMULA_VERSION,
    }
    return {
        "id": stable_id("derived_metric", identity),
        "video_id": video_id,
        "name": name,
        "value": value,
        "unit": unit,
        "formula_id": formula_id,
        "formula_version": FORMULA_VERSION,
        "source_metric_ids": source_ids,
        "source_date_range": {"start_date": start_date, "end_date": end_date},
        "calculated_at": calculated_at,
        "nullability_reason": nullability_reason,
    }


def _load_period_rows(
    workspace: Workspace, alias: str, start_date: str, end_date: str
) -> list[dict[str, Any]]:
    path = (
        workspace.data_dir
        / "normalized"
        / "youtube"
        / alias
        / "analytics_rows.json"
    )
    if not path.is_file():
        raise ValueError("No normalized YouTube analytics exist. Run analytics sync first.")
    document = workspace.read_json(path)
    records = [
        item
        for item in document.get("records", [])
        if isinstance(item, dict)
        and item.get("start_date") == start_date
        and item.get("end_date") == end_date
    ]
    if not records:
        raise ValueError(
            "No normalized analytics match the exact requested channel and date range."
        )
    return records


def analyze_performance(
    workspace: Workspace,
    alias: str,
    start_date: str,
    end_date: str,
    *,
    dry_run: bool = False,
    clock: Callable[[], str] = utc_now,
) -> dict[str, Any]:
    """Calculate period-scoped metrics without making network requests."""
    authorize(Action.LOCAL_ANALYSIS)
    validate_date_range(start_date, end_date)
    rows = _load_period_rows(workspace, alias, start_date, end_date)
    calculated_at = clock()
    metric_records: list[dict[str, Any]] = []
    by_video: dict[str, dict[str, float | None]] = {}
    source_by_video: dict[str, str] = {}
    for row in rows:
        source_id = str(row["id"])
        video_id = str(row["video_id"])
        source_by_video[video_id] = source_id
        raw = row.get("metrics", {})
        if not isinstance(raw, dict):
            raise ValueError("Normalized analytics metrics must be a JSON object.")
        source: dict[str, float | None] = {
            internal: _number(raw.get(api_name))
            for internal, api_name in _SOURCE_NAMES.items()
        }
        per_video_views = source["views"]
        subscribers_net = (
            source["subscribers_gained"] - source["subscribers_lost"]
            if source["subscribers_gained"] is not None
            and source["subscribers_lost"] is not None
            else None
        )
        engagement_total = (
            source["likes"] + source["comments"] + source["shares"]
            if source["likes"] is not None
            and source["comments"] is not None
            and source["shares"] is not None
            else None
        )
        calculated_rates = {
            "subscriber_conversion_rate": safe_rate(subscribers_net, per_video_views),
            "engagement_rate": safe_rate(engagement_total, per_video_views),
            "like_rate": safe_rate(source["likes"], per_video_views),
            "comment_rate": safe_rate(source["comments"], per_video_views),
            "share_rate": safe_rate(source["shares"], per_video_views),
        }
        values = {
            **source,
            **{name: result[0] for name, result in calculated_rates.items()},
        }
        by_video[video_id] = values
        for name, value in source.items():
            metric_records.append(
                _metric_record(
                    video_id=video_id,
                    name=name,
                    value=value,
                    unit=(
                        "percent"
                        if name
                        in {"average_percentage_viewed", "impression_click_through_rate"}
                        else "seconds"
                        if name == "average_view_duration"
                        else "minutes"
                        if name == "estimated_minutes_watched"
                        else "count"
                    ),
                    formula_id="owner_analytics_direct_v1",
                    source_ids=[source_id],
                    start_date=start_date,
                    end_date=end_date,
                    calculated_at=calculated_at,
                    nullability_reason=None if value is not None else "source_metric_missing",
                )
            )
        for name, (value, reason) in calculated_rates.items():
            metric_records.append(
                _metric_record(
                    video_id=video_id,
                    name=name,
                    value=value,
                    unit="percent",
                    formula_id=(
                        "net_subscribers_divided_by_views_v1"
                        if name == "subscriber_conversion_rate"
                        else "engagement_actions_divided_by_views_v1"
                        if name == "engagement_rate"
                        else f"{name.removesuffix('_rate')}_divided_by_views_v1"
                    ),
                    source_ids=[source_id],
                    start_date=start_date,
                    end_date=end_date,
                    calculated_at=calculated_at,
                    nullability_reason=reason,
                )
            )
    view_samples = [
        item["views"] for item in by_video.values() if item["views"] is not None
    ]
    total_views = sum(cast_value for cast_value in view_samples)
    views_per_video = (
        round(total_views / len(view_samples), 4) if view_samples else None
    )
    source_ids = [str(row["id"]) for row in rows]
    metric_records.append(
        _metric_record(
            video_id=None,
            name="views_per_video",
            value=views_per_video,
            unit="count",
            formula_id="period_views_divided_by_videos_v1",
            source_ids=source_ids,
            start_date=start_date,
            end_date=end_date,
            calculated_at=calculated_at,
            nullability_reason=None if view_samples else "no_video_view_metrics",
        )
    )
    publishing_frequency, publishing_sources = _publishing_frequency(
        workspace, alias, start_date, end_date
    )
    metric_records.append(
        _metric_record(
            video_id=None,
            name="publishing_frequency",
            value=publishing_frequency,
            unit="videos_per_week",
            formula_id="published_videos_divided_by_period_weeks_v1",
            source_ids=publishing_sources,
            start_date=start_date,
            end_date=end_date,
            calculated_at=calculated_at,
            nullability_reason=None,
        )
    )
    baseline_names = (
        "views",
        "impressions",
        "impression_click_through_rate",
        "average_percentage_viewed",
        "average_view_duration",
        "subscriber_conversion_rate",
        "engagement_rate",
        "like_rate",
        "comment_rate",
        "share_rate",
    )
    baselines: dict[str, dict[str, Any]] = {}
    for name in baseline_names:
        sample = [value[name] for value in by_video.values() if value.get(name) is not None]
        numeric = [float(value) for value in sample if value is not None]
        baselines[name] = {
            "sample_size": len(numeric),
            "median": statistics.median(numeric) if numeric else None,
            "p25": _percentile(numeric, 0.25),
            "p75": _percentile(numeric, 0.75),
            "formula_id": "channel_percentile_linear_v1",
            "formula_version": FORMULA_VERSION,
            "source_metric_ids": source_ids,
            "source_date_range": {
                "start_date": start_date,
                "end_date": end_date,
            },
            "calculated_at": calculated_at,
            "nullability_reason": None if numeric else "no_compatible_values",
        }
    views_median = _number(baselines["views"]["median"])
    for video_id, values in by_video.items():
        video_views = values.get("views")
        if video_views is None:
            relative_value, relative_reason = None, "source_metric_missing"
        elif views_median is None:
            relative_value, relative_reason = None, "baseline_missing"
        elif views_median == 0:
            relative_value, relative_reason = None, "zero_baseline"
        else:
            relative_value = round((video_views - views_median) / views_median * 100, 4)
            relative_reason = None
        metric_records.append(
            _metric_record(
                video_id=video_id,
                name="relative_views_vs_channel_median",
                value=relative_value,
                unit="percent",
                formula_id="views_minus_median_divided_by_median_v1",
                source_ids=[source_by_video[video_id]],
                start_date=start_date,
                end_date=end_date,
                calculated_at=calculated_at,
                nullability_reason=relative_reason,
            )
        )
    analysis_id = stable_id(
        "youtube_analysis",
        {"channel": alias, "start_date": start_date, "end_date": end_date},
    )
    result = {
        "id": analysis_id,
        "schema_version": 1,
        "channel": alias,
        "start_date": start_date,
        "end_date": end_date,
        "state": "succeeded",
        "calculated_at": calculated_at,
        "network_requests": 0,
        "formula_version": FORMULA_VERSION,
        "formulas": {
            "engagement_rate": "(likes + comments + shares) / views * 100",
            "subscriber_conversion_rate": "(subscribers gained - subscribers lost) / views * 100",
            "publishing_frequency": "videos published in period / period days * 7",
            "relative_views_vs_channel_median": "(video views - median views) / median views * 100",
        },
        "metrics": metric_records,
        "baselines": baselines,
    }
    if not dry_run:
        _upsert_analysis(
            workspace,
            f"derived/youtube/{alias}/metrics.json",
            result,
            calculated_at,
        )
        workspace.audit(
            "youtube_performance_analyzed",
            {
                "channel": alias,
                "start_date": start_date,
                "end_date": end_date,
                "analysis_id": analysis_id,
                "network_requests": 0,
            },
        )
    return result


def _publishing_frequency(
    workspace: Workspace, alias: str, start_date: str, end_date: str
) -> tuple[float, list[str]]:
    path = workspace.data_dir / "normalized" / "youtube" / alias / "videos.json"
    records: list[dict[str, Any]] = []
    if path.is_file():
        document = workspace.read_json(path)
        records = [item for item in document.get("records", []) if isinstance(item, dict)]
    start = date.fromisoformat(start_date)
    end = date.fromisoformat(end_date)
    included: list[dict[str, Any]] = []
    for record in records:
        published = record.get("published_at")
        if not isinstance(published, str):
            continue
        try:
            published_date = datetime.fromisoformat(published.replace("Z", "+00:00")).date()
        except ValueError:
            continue
        if start <= published_date <= end:
            included.append(record)
    days = (end - start).days + 1
    return round(len(included) / days * 7, 4), [str(item["id"]) for item in included]


def _upsert_analysis(
    workspace: Workspace, relative: str, result: dict[str, Any], updated_at: str
) -> None:
    path = workspace.data_dir / relative
    analyses: list[dict[str, Any]] = []
    if path.is_file():
        document = workspace.read_json(path)
        analyses = [
            item for item in document.get("analyses", []) if isinstance(item, dict)
        ]
    by_id = {str(item["id"]): item for item in analyses}
    by_id[str(result["id"])] = result
    workspace.write_json(
        relative,
        {
            "schema_version": 1,
            "updated_at": updated_at,
            "analyses": [by_id[key] for key in sorted(by_id)],
        },
    )


def _load_analysis(
    workspace: Workspace, alias: str, start_date: str, end_date: str
) -> dict[str, Any]:
    path = workspace.data_dir / "derived" / "youtube" / alias / "metrics.json"
    if not path.is_file():
        raise ValueError("No derived YouTube metrics exist. Run analytics analyze first.")
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
        raise ValueError("No derived metrics match the exact requested date range.")
    return result


def diagnose_content(
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
    analysis = _load_analysis(workspace, alias, start_date, end_date)
    metric_values: dict[str, dict[str, float | None]] = {}
    source_ids: dict[str, list[str]] = {}
    for metric in analysis.get("metrics", []):
        if not isinstance(metric, dict) or metric.get("video_id") is None:
            continue
        video_id = str(metric["video_id"])
        metric_values.setdefault(video_id, {})[str(metric["name"])] = _number(
            metric.get("value")
        )
        source_ids.setdefault(video_id, []).append(str(metric["id"]))
    baselines = analysis.get("baselines", {})
    diagnoses = [
        diagnose_video(
            video_id,
            values,
            baselines if isinstance(baselines, dict) else {},
            source_ids.get(video_id, []),
        )
        for video_id, values in sorted(metric_values.items())
    ]
    diagnosis_id = stable_id(
        "youtube_diagnoses",
        {"channel": alias, "start_date": start_date, "end_date": end_date},
    )
    result = {
        "id": diagnosis_id,
        "schema_version": 1,
        "channel": alias,
        "start_date": start_date,
        "end_date": end_date,
        "state": "succeeded",
        "calculated_at": clock(),
        "network_requests": 0,
        "rule_version": RULE_VERSION,
        "fallback_thresholds": FALLBACK_THRESHOLDS,
        "diagnoses": diagnoses,
    }
    if not dry_run:
        _upsert_analysis(
            workspace,
            f"derived/youtube/{alias}/diagnoses.json",
            result,
            str(result["calculated_at"]),
        )
        workspace.audit(
            "youtube_content_diagnosed",
            {
                "channel": alias,
                "start_date": start_date,
                "end_date": end_date,
                "diagnosis_id": diagnosis_id,
                "rule_version": RULE_VERSION,
                "network_requests": 0,
            },
        )
    return result


def _baseline(
    baselines: dict[str, Any], name: str
) -> tuple[float, str, int]:
    raw = baselines.get(name, {})
    if isinstance(raw, dict):
        sample = int(raw.get("sample_size", 0))
        median = _number(raw.get("median"))
        if sample >= 3 and median is not None:
            return median, "channel_median", sample
    return FALLBACK_THRESHOLDS[name], "documented_fallback", 0


def diagnose_video(
    video_id: str,
    metrics: dict[str, float | None],
    baselines: dict[str, Any],
    source_ids: list[str],
) -> dict[str, Any]:
    views = metrics.get("views")
    impressions = metrics.get("impressions")
    ctr = metrics.get("impression_click_through_rate")
    retention = metrics.get("average_percentage_viewed")
    conversion = metrics.get("subscriber_conversion_rate")
    engagement = metrics.get("engagement_rate")
    required = [views, impressions, ctr, retention]
    evidence: list[dict[str, Any]] = []
    if sum(value is not None for value in required) < 3:
        category = "insufficient_data"
        observations = ["Too few compatible owner-analytics metrics were available."]
        recommendations = ["Collect a longer date range before changing content strategy."]
        confidence = "low"
    else:
        impression_base, impression_source, sample = _baseline(
            baselines, "impressions"
        )
        ctr_base, ctr_source, _ = _baseline(
            baselines, "impression_click_through_rate"
        )
        retention_base, retention_source, _ = _baseline(
            baselines, "average_percentage_viewed"
        )
        conversion_base, conversion_source, _ = _baseline(
            baselines, "subscriber_conversion_rate"
        )
        engagement_base, engagement_source, _ = _baseline(
            baselines, "engagement_rate"
        )
        comparisons = (
            ("impressions", impressions, impression_base, impression_source),
            ("impression_click_through_rate", ctr, ctr_base, ctr_source),
            ("average_percentage_viewed", retention, retention_base, retention_source),
            ("subscriber_conversion_rate", conversion, conversion_base, conversion_source),
            ("engagement_rate", engagement, engagement_base, engagement_source),
        )
        evidence = [
            {
                "metric": name,
                "value": value,
                "baseline": baseline,
                "baseline_source": source,
                "comparison": (
                    "unavailable"
                    if value is None
                    else "above_or_equal"
                    if value >= baseline
                    else "below"
                ),
            }
            for name, value, baseline, source in comparisons
        ]
        low_reach = impressions is not None and impressions < impression_base
        strong_ctr = ctr is not None and ctr >= ctr_base
        strong_retention = retention is not None and retention >= retention_base
        strong_engagement = engagement is not None and engagement >= engagement_base
        if impressions is not None and impressions < impression_base * 0.5:
            category = "low_impressions"
            observations = ["Impressions were materially below the comparison baseline."]
            recommendations = ["Test distribution and topic demand before changing the content."]
        elif ctr is not None and ctr < ctr_base * 0.8:
            category = "weak_click_through_rate"
            observations = ["Click-through rate was below the comparison baseline."]
            recommendations = ["Test a clearer title and thumbnail promise."]
        elif strong_ctr and retention is not None and retention < retention_base:
            category = "strong_packaging_low_retention"
            observations = ["Packaging earned clicks, but retention trailed the baseline."]
            recommendations = ["Align the opening and structure more closely with the promise."]
        elif retention is not None and retention < retention_base * 0.8:
            category = "weak_opening_or_retention"
            observations = ["Average percentage viewed was materially below baseline."]
            recommendations = ["Review the opening pace and remove delayed context."]
        elif strong_retention and low_reach:
            category = "strong_retention_low_distribution"
            observations = ["Retention exceeded baseline while impressions remained low."]
            recommendations = [
                "Test packaging and distribution before changing the content structure."
            ]
        elif (
            impressions is not None
            and impressions >= impression_base
            and conversion is not None
            and conversion < conversion_base
        ):
            category = "high_reach_low_subscriber_conversion"
            observations = ["Reach was healthy, but subscriber conversion trailed baseline."]
            recommendations = ["Clarify the channel-level value viewers can expect next."]
        elif strong_engagement and low_reach:
            category = "high_engagement_low_reach"
            observations = ["Engagement exceeded baseline while distribution remained limited."]
            recommendations = ["Retest the topic with stronger discovery-oriented packaging."]
        elif strong_ctr and strong_retention and not low_reach:
            category = "strong_overall_performance"
            observations = ["Reach, click-through rate, and retention were healthy."]
            recommendations = ["Reuse the underlying audience promise without copying the asset."]
        else:
            category = "inconclusive_mixed_signals"
            observations = ["The available signals did not support one dominant diagnosis."]
            recommendations = ["Run one controlled packaging or opening experiment at a time."]
        confidence = "high" if sample >= 5 else "medium"
    return {
        "id": stable_id(
            "content_diagnosis",
            {"video_id": video_id, "rule_version": RULE_VERSION, "sources": source_ids},
        ),
        "video_id": video_id,
        "diagnosis": category,
        "confidence": confidence,
        "evidence": evidence,
        "observations": observations,
        "recommendations": recommendations,
        "source_metric_ids": source_ids,
        "rule_version": RULE_VERSION,
    }
