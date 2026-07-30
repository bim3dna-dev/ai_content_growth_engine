"""Deterministic normalization of official YouTube API responses."""

from __future__ import annotations

import re
from datetime import timedelta
from typing import Any

from growth_engine.storage import stable_id

_DURATION = re.compile(
    r"^P(?:(?P<days>\d+)D)?(?:T(?:(?P<hours>\d+)H)?"
    r"(?:(?P<minutes>\d+)M)?(?:(?P<seconds>\d+(?:\.\d+)?)S)?)?$"
)


def parse_iso8601_duration(value: object) -> int | None:
    """Parse the YouTube subset of ISO 8601 duration into whole seconds."""
    if not isinstance(value, str):
        return None
    match = _DURATION.fullmatch(value)
    if match is None:
        return None
    parts = match.groupdict(default="0")
    seconds = timedelta(
        days=int(parts["days"]),
        hours=int(parts["hours"]),
        minutes=int(parts["minutes"]),
        seconds=float(parts["seconds"]),
    ).total_seconds()
    return int(seconds)


def _mapping(value: object) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _text(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _integer(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return None
    return None


def _boolean(value: object) -> bool | None:
    return value if isinstance(value, bool) else None


def _thumbnail_url(snippet: dict[str, Any], preferred: str = "default") -> str | None:
    thumbnails = _mapping(snippet.get("thumbnails"))
    selected = _mapping(thumbnails.get(preferred))
    if not selected and thumbnails:
        selected = _mapping(next(iter(thumbnails.values())))
    return _text(selected.get("url"))


def normalize_channel(
    response: dict[str, Any], *, raw_snapshot_id: str, normalized_at: str
) -> dict[str, Any]:
    items = response.get("items")
    if not isinstance(items, list) or not items or not isinstance(items[0], dict):
        raise ValueError("Owned-channel response did not contain a channel resource.")
    item = items[0]
    channel_id = _text(item.get("id"))
    if not channel_id:
        raise ValueError("Owned-channel response did not contain a channel ID.")
    snippet = _mapping(item.get("snippet"))
    statistics = _mapping(item.get("statistics"))
    content = _mapping(item.get("contentDetails"))
    related = _mapping(content.get("relatedPlaylists"))
    thumbnails = _mapping(snippet.get("thumbnails"))
    thumbnail_urls = {
        key: value["url"]
        for key, raw in thumbnails.items()
        if isinstance(key, str)
        and isinstance(raw, dict)
        and isinstance((value := raw).get("url"), str)
    }
    return {
        "id": stable_id("yt_channel", {"channel_id": channel_id}),
        "platform": "youtube",
        "channel_id": channel_id,
        "title": _text(snippet.get("title")),
        "description": _text(snippet.get("description")),
        "custom_url": _text(snippet.get("customUrl")),
        "created_at": _text(snippet.get("publishedAt")),
        "thumbnail_urls": thumbnail_urls,
        "country": _text(snippet.get("country")),
        "subscriber_count": _integer(statistics.get("subscriberCount")),
        "hidden_subscriber_count": _boolean(statistics.get("hiddenSubscriberCount")),
        "view_count": _integer(statistics.get("viewCount")),
        "video_count": _integer(statistics.get("videoCount")),
        "uploads_playlist_id": _text(related.get("uploads")),
        "raw_snapshot_id": raw_snapshot_id,
        "normalized_at": normalized_at,
        "schema_version": 1,
    }


def normalize_videos(
    requested_ids: list[str],
    response: dict[str, Any],
    *,
    channel_id: str,
    raw_snapshot_id: str,
    normalized_at: str,
) -> list[dict[str, Any]]:
    returned = {
        item["id"]: item
        for item in response.get("items", [])
        if isinstance(item, dict) and isinstance(item.get("id"), str)
    }
    records: list[dict[str, Any]] = []
    for video_id in requested_ids:
        item = returned.get(video_id)
        base: dict[str, Any] = {
            "id": stable_id("yt_video", {"video_id": video_id}),
            "platform": "youtube",
            "video_id": video_id,
            "channel_id": channel_id,
            "raw_snapshot_id": raw_snapshot_id,
            "normalized_at": normalized_at,
            "schema_version": 1,
        }
        if item is None:
            records.append(
                {
                    **base,
                    "availability": "unavailable_or_deleted",
                    "title": None,
                    "description": None,
                    "published_at": None,
                    "thumbnail_url": None,
                    "duration_seconds": None,
                    "privacy_status": None,
                    "upload_status": None,
                    "made_for_kids": None,
                    "self_declared_made_for_kids": None,
                    "tags": None,
                    "category_id": None,
                    "default_language": None,
                    "default_audio_language": None,
                    "live_broadcast_state": None,
                    "view_count": None,
                    "like_count": None,
                    "comment_count": None,
                }
            )
            continue
        snippet = _mapping(item.get("snippet"))
        details = _mapping(item.get("contentDetails"))
        status = _mapping(item.get("status"))
        statistics = _mapping(item.get("statistics"))
        tags = snippet.get("tags")
        records.append(
            {
                **base,
                "availability": "available",
                "title": _text(snippet.get("title")),
                "description": _text(snippet.get("description")),
                "published_at": _text(snippet.get("publishedAt")),
                "thumbnail_url": _thumbnail_url(snippet),
                "duration_seconds": parse_iso8601_duration(details.get("duration")),
                "privacy_status": _text(status.get("privacyStatus")),
                "upload_status": _text(status.get("uploadStatus")),
                "made_for_kids": _boolean(status.get("madeForKids")),
                "self_declared_made_for_kids": _boolean(
                    status.get("selfDeclaredMadeForKids")
                ),
                "tags": [str(tag) for tag in tags] if isinstance(tags, list) else None,
                "category_id": _text(snippet.get("categoryId")),
                "default_language": _text(snippet.get("defaultLanguage")),
                "default_audio_language": _text(snippet.get("defaultAudioLanguage")),
                "live_broadcast_state": _text(snippet.get("liveBroadcastContent")),
                "view_count": _integer(statistics.get("viewCount")),
                "like_count": _integer(statistics.get("likeCount")),
                "comment_count": _integer(statistics.get("commentCount")),
            }
        )
    return records


def normalize_analytics_rows(
    response: dict[str, Any],
    *,
    channel_id: str,
    start_date: str,
    end_date: str,
    raw_snapshot_id: str,
    normalized_at: str,
) -> tuple[list[dict[str, Any]], list[str]]:
    headers = response.get("columnHeaders", [])
    if not isinstance(headers, list):
        raise ValueError("Analytics response columnHeaders must be a list.")
    names: list[str] = []
    for header in headers:
        if isinstance(header, dict):
            name = header.get("name")
            if isinstance(name, str):
                names.append(name)
    if not names or names[0] != "video":
        raise ValueError("Analytics response must use video as its primary dimension.")
    raw_rows = response.get("rows", [])
    if raw_rows is None:
        raw_rows = []
    if not isinstance(raw_rows, list):
        raise ValueError("Analytics response rows must be a list.")
    records: list[dict[str, Any]] = []
    for raw in raw_rows:
        if not isinstance(raw, list) or len(raw) != len(names):
            raise ValueError("Analytics row length does not match column headers.")
        values = dict(zip(names, raw, strict=True))
        video_id = values.get("video")
        if not isinstance(video_id, str):
            raise ValueError("Analytics row contains an invalid video dimension.")
        source_key = {
            "video_id": video_id,
            "start_date": start_date,
            "end_date": end_date,
        }
        records.append(
            {
                "id": stable_id("yt_analytics", source_key),
                "platform": "youtube",
                "channel_id": channel_id,
                "video_id": video_id,
                "start_date": start_date,
                "end_date": end_date,
                "metrics": {name: values[name] for name in names[1:]},
                "api_metric_names": names[1:],
                "raw_snapshot_id": raw_snapshot_id,
                "normalized_at": normalized_at,
                "schema_version": 1,
            }
        )
    return records, names
