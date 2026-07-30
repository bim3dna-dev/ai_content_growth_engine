"""Read-only YouTube configuration, authorization, and synchronization workflows."""

from __future__ import annotations

from collections.abc import Callable
from datetime import date
from pathlib import Path
from typing import Any
from uuid import uuid4

from growth_engine.policy import Action, PolicyViolation, authorize
from growth_engine.storage import Workspace, utc_now
from growth_engine.youtube.api import (
    ApiFailure,
    GoogleOAuthProvider,
    OAuthProvider,
    YouTubeAnalyticsReader,
    YouTubeDataReader,
    load_google_readers,
)
from growth_engine.youtube.config import (
    configure_channel,
    get_channel_config,
    pin_expected_channel,
)
from growth_engine.youtube.normalize import (
    normalize_analytics_rows,
    normalize_channel,
    normalize_videos,
)

ANALYTICS_METRICS = (
    "views",
    "estimatedMinutesWatched",
    "averageViewDuration",
    "averageViewPercentage",
    "subscribersGained",
    "subscribersLost",
    "likes",
    "comments",
    "shares",
    "videoThumbnailImpressions",
    "videoThumbnailImpressionsClickRate",
)

TERMINAL_STATES = frozenset(
    {
        "succeeded",
        "partially_succeeded",
        "failed",
        "skipped",
        "blocked_by_policy",
        "not_authorized",
    }
)


def _retrieval_id(kind: str, clock: Callable[[], str], nonce: Callable[[], str]) -> str:
    timestamp = clock().replace(":", "").replace("+", "_").replace("-", "")
    return f"{kind}_{timestamp}_{nonce()}"


def _nonce() -> str:
    return uuid4().hex


def _require_config(workspace: Workspace, alias: str) -> dict[str, Any]:
    config = get_channel_config(workspace, alias)
    if config is None:
        raise ValueError(
            f"YouTube channel '{alias}' is not configured. Run 'growth-engine youtube configure'."
        )
    return config


def _require_authorized(
    channel_config: dict[str, Any], provider: OAuthProvider
) -> None:
    status = provider.credential_status(channel_config)
    if status != "authorized":
        raise ValueError(f"YouTube channel is not authorized (status: {status}).")


def _load_readers(
    workspace: Workspace,
    config: dict[str, Any],
    oauth: OAuthProvider,
) -> tuple[YouTubeDataReader, YouTubeAnalyticsReader]:
    return load_google_readers(
        config,
        oauth,
        audit_callback=lambda event, details: workspace.audit(event, details),
    )


def configure_youtube(
    workspace: Workspace,
    alias: str,
    client_secrets: Path,
    *,
    dry_run: bool = False,
) -> dict[str, Any]:
    return configure_channel(
        workspace, alias, client_secrets, dry_run=dry_run
    )


def authorize_youtube(
    workspace: Workspace,
    alias: str,
    *,
    provider: OAuthProvider | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    authorize(Action.OFFICIAL_API_READ, official_api=True)
    config = _require_config(workspace, alias)
    if dry_run:
        return {
            "channel": alias,
            "state": "skipped",
            "reason": "dry_run",
            "scopes": "minimum_read_only",
        }
    oauth = provider or GoogleOAuthProvider()
    status = oauth.authorize(config)
    workspace.audit(
        "youtube_authorized",
        {"channel": alias, "status": status, "scopes": "minimum_read_only"},
    )
    return {"channel": alias, "state": status, "scopes": "minimum_read_only"}


def youtube_status(
    workspace: Workspace,
    alias: str,
    *,
    provider: OAuthProvider | None = None,
    data_reader: YouTubeDataReader | None = None,
) -> dict[str, Any]:
    config = get_channel_config(workspace, alias)
    if config is None:
        return {"channel": alias, "status": "not_configured"}
    oauth = provider or GoogleOAuthProvider()
    credential_status = oauth.credential_status(config)
    if credential_status != "authorized":
        return {"channel": alias, "status": credential_status}
    try:
        reader = data_reader
        if reader is None:
            reader, _ = _load_readers(workspace, config, oauth)
        response = reader.get_owned_channel()
        items = response.get("items", [])
        if not isinstance(items, list) or not items or not isinstance(items[0], dict):
            return {"channel": alias, "status": "invalid_or_revoked_authorization"}
        actual = items[0].get("id")
        expected = config.get("expected_channel_id")
        if expected and actual != expected:
            return {
                "channel": alias,
                "status": "channel_identity_mismatch",
                "expected_channel_id": expected,
                "authorized_channel_id": actual,
            }
        return {
            "channel": alias,
            "status": "authorized",
            "channel_id": actual,
        }
    except ApiFailure as exc:
        status = {
            "quota_or_rate_limited": "quota_or_rate_limit_response",
            "forbidden": "invalid_or_revoked_authorization",
            "api_unavailable": "api_unavailable",
            "invalid_or_revoked_authorization": "invalid_or_revoked_authorization",
        }.get(exc.category, "api_unavailable")
        return {"channel": alias, "status": status, "http_status": exc.status_code}


def _raw_snapshot(
    *,
    snapshot_id: str,
    kind: str,
    alias: str,
    retrieved_at: str,
    response: object,
    provenance: dict[str, Any],
) -> dict[str, Any]:
    return {
        "id": snapshot_id,
        "schema_version": 1,
        "platform": "youtube",
        "channel_alias": alias,
        "kind": kind,
        "retrieved_at": retrieved_at,
        "provenance": provenance,
        "response": response,
    }


def sync_channel(
    workspace: Workspace,
    alias: str,
    *,
    provider: OAuthProvider | None = None,
    data_reader: YouTubeDataReader | None = None,
    dry_run: bool = False,
    clock: Callable[[], str] = utc_now,
    nonce: Callable[[], str] = _nonce,
) -> dict[str, Any]:
    authorize(Action.OFFICIAL_API_READ, official_api=True)
    config = _require_config(workspace, alias)
    if dry_run:
        return {
            "channel": alias,
            "operation": "sync_channel",
            "state": "skipped",
            "reason": "dry_run",
        }
    oauth = provider or GoogleOAuthProvider()
    _require_authorized(config, oauth)
    reader = data_reader
    if reader is None:
        reader, _ = _load_readers(workspace, config, oauth)
    response = reader.get_owned_channel()
    retrieved_at = clock()
    snapshot_id = _retrieval_id("channel", clock, nonce)
    normalized = normalize_channel(
        response, raw_snapshot_id=snapshot_id, normalized_at=retrieved_at
    )
    expected = config.get("expected_channel_id")
    if expected and expected != normalized["channel_id"]:
        raise ValueError(
            f"Channel identity mismatch: expected '{expected}', "
            f"authorized '{normalized['channel_id']}'."
        )
    snapshot = _raw_snapshot(
        snapshot_id=snapshot_id,
        kind="channel",
        alias=alias,
        retrieved_at=retrieved_at,
        response=response,
        provenance={"api": "YouTube Data API v3", "method": "channels.list", "mine": True},
    )
    workspace.write_artifact(f"raw/youtube/{alias}/channel", snapshot_id, snapshot)
    workspace.write_json(f"normalized/youtube/{alias}/channels.json", normalized)
    pin_expected_channel(workspace, alias, str(normalized["channel_id"]))
    workspace.audit(
        "youtube_channel_synced",
        {
            "channel": alias,
            "channel_id": normalized["channel_id"],
            "raw_snapshot_id": snapshot_id,
            "api": "youtube_data_v3",
            "method": "channels.list",
        },
    )
    return {
        "channel": alias,
        "operation": "sync_channel",
        "state": "succeeded",
        "raw_snapshot_id": snapshot_id,
        "normalized_id": normalized["id"],
        "channel_id": normalized["channel_id"],
    }


def _normalized_channel(workspace: Workspace, alias: str) -> dict[str, Any]:
    path = workspace.data_dir / "normalized" / "youtube" / alias / "channels.json"
    if not path.is_file():
        raise ValueError("No normalized channel exists. Run YouTube channel sync first.")
    return workspace.read_json(path)


def sync_videos(
    workspace: Workspace,
    alias: str,
    max_items: int,
    *,
    provider: OAuthProvider | None = None,
    data_reader: YouTubeDataReader | None = None,
    dry_run: bool = False,
    clock: Callable[[], str] = utc_now,
    nonce: Callable[[], str] = _nonce,
) -> dict[str, Any]:
    authorize(Action.OFFICIAL_API_READ, official_api=True)
    if max_items < 1 or max_items > 500:
        raise ValueError("--max-items must be between 1 and 500.")
    config = _require_config(workspace, alias)
    channel = _normalized_channel(workspace, alias)
    uploads = channel.get("uploads_playlist_id")
    if not isinstance(uploads, str) or not uploads:
        raise ValueError("Normalized channel does not expose an uploads playlist ID.")
    if dry_run:
        return {
            "channel": alias,
            "operation": "sync_videos",
            "state": "skipped",
            "reason": "dry_run",
        }
    oauth = provider or GoogleOAuthProvider()
    _require_authorized(config, oauth)
    reader = data_reader
    if reader is None:
        reader, _ = _load_readers(workspace, config, oauth)
    playlist = reader.list_upload_video_ids(uploads, max_items)
    ids = playlist.get("video_ids", [])
    if not isinstance(ids, list) or not all(isinstance(item, str) for item in ids):
        raise ValueError("Uploads-playlist response contained invalid video IDs.")
    videos = reader.get_videos(ids)
    retrieved_at = clock()
    playlist_snapshot_id = _retrieval_id("playlist", clock, nonce)
    videos_snapshot_id = _retrieval_id("videos", clock, nonce)
    workspace.write_artifact(
        f"raw/youtube/{alias}/playlist_items",
        playlist_snapshot_id,
        _raw_snapshot(
            snapshot_id=playlist_snapshot_id,
            kind="playlist_items",
            alias=alias,
            retrieved_at=retrieved_at,
            response=playlist,
            provenance={
                "api": "YouTube Data API v3",
                "method": "playlistItems.list",
                "playlist_id": uploads,
                "pagination": True,
            },
        ),
    )
    workspace.write_artifact(
        f"raw/youtube/{alias}/videos",
        videos_snapshot_id,
        _raw_snapshot(
            snapshot_id=videos_snapshot_id,
            kind="videos",
            alias=alias,
            retrieved_at=retrieved_at,
            response=videos,
            provenance={
                "api": "YouTube Data API v3",
                "method": "videos.list",
                "batch_size": 50,
            },
        ),
    )
    normalized = normalize_videos(
        ids,
        videos,
        channel_id=str(channel["channel_id"]),
        raw_snapshot_id=videos_snapshot_id,
        normalized_at=retrieved_at,
    )
    normalized_path = (
        workspace.data_dir / "normalized" / "youtube" / alias / "videos.json"
    )
    existing_records: list[dict[str, Any]] = []
    if normalized_path.is_file():
        existing = workspace.read_json(normalized_path)
        existing_records = [
            item for item in existing.get("records", []) if isinstance(item, dict)
        ]
    by_video = {str(item["video_id"]): item for item in existing_records}
    by_video.update({str(item["video_id"]): item for item in normalized})
    document = {
        "schema_version": 1,
        "channel_alias": alias,
        "updated_at": retrieved_at,
        "records": [by_video[key] for key in sorted(by_video)],
    }
    workspace.write_json(f"normalized/youtube/{alias}/videos.json", document)
    unavailable = sum(item["availability"] != "available" for item in normalized)
    workspace.audit(
        "youtube_videos_synced",
        {
            "channel": alias,
            "playlist_snapshot_id": playlist_snapshot_id,
            "videos_snapshot_id": videos_snapshot_id,
            "requested": len(ids),
            "normalized": len(normalized),
            "unavailable": unavailable,
            "api": "youtube_data_v3",
        },
    )
    return {
        "channel": alias,
        "operation": "sync_videos",
        "state": "succeeded",
        "playlist_snapshot_id": playlist_snapshot_id,
        "videos_snapshot_id": videos_snapshot_id,
        "video_count": len(normalized),
        "unavailable_count": unavailable,
        "normalized_total": len(by_video),
    }


def validate_date_range(start_date: str, end_date: str) -> None:
    try:
        start = date.fromisoformat(start_date)
        end = date.fromisoformat(end_date)
    except ValueError as exc:
        raise ValueError("Dates must use YYYY-MM-DD format.") from exc
    if start > end:
        raise ValueError("Start date must not be after end date.")


def _query_analytics_with_fallback(
    reader: YouTubeAnalyticsReader,
    start_date: str,
    end_date: str,
) -> tuple[dict[str, Any], list[str]]:
    try:
        return (
            reader.query_video_metrics(start_date, end_date, ANALYTICS_METRICS),
            [],
        )
    except ApiFailure as exc:
        if exc.category != "invalid_request":
            raise
    successful: dict[str, dict[str, object]] = {}
    unsupported: list[str] = []
    for metric in ANALYTICS_METRICS:
        try:
            response = reader.query_video_metrics(start_date, end_date, (metric,))
        except ApiFailure as exc:
            if exc.category == "invalid_request":
                unsupported.append(metric)
                continue
            raise
        headers = response.get("columnHeaders", [])
        rows = response.get("rows", []) or []
        if not isinstance(headers, list) or not isinstance(rows, list):
            raise ValueError("Analytics fallback response is malformed.")
        names = [
            header.get("name")
            for header in headers
            if isinstance(header, dict) and isinstance(header.get("name"), str)
        ]
        if names != ["video", metric]:
            raise ValueError(
                f"Analytics fallback response for '{metric}' returned incompatible columns."
            )
        for row in rows:
            if not isinstance(row, list) or len(row) != 2 or not isinstance(row[0], str):
                raise ValueError("Analytics fallback response contains a malformed row.")
            successful.setdefault(row[0], {})[metric] = row[1]
    supported = [metric for metric in ANALYTICS_METRICS if metric not in unsupported]
    if not supported:
        unsupported_names = ", ".join(unsupported)
        raise ValueError(
            f"No requested Analytics metrics were supported: {unsupported_names}."
        )
    combined_rows = [
        [video_id, *[values.get(metric) for metric in supported]]
        for video_id, values in sorted(successful.items())
    ]
    return (
        {
            "kind": "youtubeAnalytics#resultTable",
            "columnHeaders": [
                {"name": "video", "columnType": "DIMENSION", "dataType": "STRING"},
                *[
                    {"name": metric, "columnType": "METRIC", "dataType": "UNKNOWN"}
                    for metric in supported
                ],
            ],
            "rows": combined_rows,
            "partial_metric_fallback": True,
        },
        unsupported,
    )


def sync_analytics(
    workspace: Workspace,
    alias: str,
    start_date: str,
    end_date: str,
    *,
    provider: OAuthProvider | None = None,
    analytics_reader: YouTubeAnalyticsReader | None = None,
    dry_run: bool = False,
    clock: Callable[[], str] = utc_now,
    nonce: Callable[[], str] = _nonce,
) -> dict[str, Any]:
    authorize(Action.OFFICIAL_API_READ, official_api=True)
    validate_date_range(start_date, end_date)
    config = _require_config(workspace, alias)
    channel = _normalized_channel(workspace, alias)
    if dry_run:
        return {
            "channel": alias,
            "operation": "sync_analytics",
            "state": "skipped",
            "reason": "dry_run",
            "start_date": start_date,
            "end_date": end_date,
        }
    oauth = provider or GoogleOAuthProvider()
    _require_authorized(config, oauth)
    reader = analytics_reader
    if reader is None:
        _, reader = _load_readers(workspace, config, oauth)
    response, unsupported_metrics = _query_analytics_with_fallback(
        reader, start_date, end_date
    )
    retrieved_at = clock()
    snapshot_id = _retrieval_id("analytics", clock, nonce)
    rows, headers = normalize_analytics_rows(
        response,
        channel_id=str(channel["channel_id"]),
        start_date=start_date,
        end_date=end_date,
        raw_snapshot_id=snapshot_id,
        normalized_at=retrieved_at,
    )
    workspace.write_artifact(
        f"raw/youtube/{alias}/analytics",
        snapshot_id,
        _raw_snapshot(
            snapshot_id=snapshot_id,
            kind="analytics",
            alias=alias,
            retrieved_at=retrieved_at,
            response=response,
            provenance={
                "api": "YouTube Analytics API v2",
                "method": "reports.query",
                "start_date": start_date,
                "end_date": end_date,
                "dimensions": ["video"],
                "metrics": list(ANALYTICS_METRICS),
                "unsupported_metrics": unsupported_metrics,
                "filters": [],
                "column_headers": headers,
            },
        ),
    )
    normalized_path = (
        workspace.data_dir / "normalized" / "youtube" / alias / "analytics_rows.json"
    )
    existing_records: list[dict[str, Any]] = []
    if normalized_path.is_file():
        existing = workspace.read_json(normalized_path)
        existing_records = [
            item for item in existing.get("records", []) if isinstance(item, dict)
        ]
    by_id = {str(item["id"]): item for item in existing_records}
    by_id.update({str(item["id"]): item for item in rows})
    document = {
        "schema_version": 1,
        "channel_alias": alias,
        "updated_at": retrieved_at,
        "records": [by_id[key] for key in sorted(by_id)],
    }
    workspace.write_json(
        f"normalized/youtube/{alias}/analytics_rows.json", document
    )
    workspace.audit(
        "youtube_analytics_synced",
        {
            "channel": alias,
            "raw_snapshot_id": snapshot_id,
            "start_date": start_date,
            "end_date": end_date,
            "row_count": len(rows),
            "metrics": list(ANALYTICS_METRICS),
            "unsupported_metrics": unsupported_metrics,
            "api": "youtube_analytics_v2",
        },
    )
    return {
        "channel": alias,
        "operation": "sync_analytics",
        "state": "succeeded",
        "raw_snapshot_id": snapshot_id,
        "row_count": len(rows),
        "normalized_total": len(by_id),
        "start_date": start_date,
        "end_date": end_date,
        "api_metric_names": headers[1:],
        "unsupported_metrics": unsupported_metrics,
    }


def sync_all(
    workspace: Workspace,
    alias: str,
    start_date: str,
    end_date: str,
    max_items: int,
    *,
    provider: OAuthProvider | None = None,
    data_reader: YouTubeDataReader | None = None,
    analytics_reader: YouTubeAnalyticsReader | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    validate_date_range(start_date, end_date)
    steps: list[dict[str, Any]] = []
    operations: tuple[Callable[[], dict[str, Any]], ...] = (
        lambda: sync_channel(
            workspace,
            alias,
            provider=provider,
            data_reader=data_reader,
            dry_run=dry_run,
        ),
        lambda: sync_videos(
            workspace,
            alias,
            max_items,
            provider=provider,
            data_reader=data_reader,
            dry_run=dry_run,
        ),
        lambda: sync_analytics(
            workspace,
            alias,
            start_date,
            end_date,
            provider=provider,
            analytics_reader=analytics_reader,
            dry_run=dry_run,
        ),
    )
    for operation in operations:
        try:
            steps.append(operation())
        except PolicyViolation as exc:
            steps.append(
                {
                    "state": "blocked_by_policy",
                    "category": "policy",
                    "message": str(exc),
                }
            )
            break
        except ValueError as exc:
            step_state = (
                "not_authorized" if "not authorized" in str(exc).lower() else "failed"
            )
            steps.append(
                {"state": step_state, "category": step_state, "message": str(exc)}
            )
            break
        except Exception as exc:
            category = exc.category if isinstance(exc, ApiFailure) else "failed"
            steps.append({"state": "failed", "category": category, "message": str(exc)})
            break
    states = [str(item["state"]) for item in steps]
    if states and all(state == "succeeded" for state in states) and len(steps) == 3:
        state = "succeeded"
    elif states and all(state == "skipped" for state in states) and len(steps) == 3:
        state = "skipped"
    elif any(state == "succeeded" for state in states):
        state = "partially_succeeded"
    elif "blocked_by_policy" in states:
        state = "blocked_by_policy"
    elif "not_authorized" in states:
        state = "not_authorized"
    else:
        state = "failed"
    if state not in TERMINAL_STATES:
        raise AssertionError(f"Unexpected full-sync terminal state: {state}")
    workspace.audit(
        "youtube_full_sync_finished",
        {"channel": alias, "state": state, "step_states": states},
    ) if not dry_run else None
    return {"channel": alias, "operation": "sync_all", "state": state, "steps": steps}
