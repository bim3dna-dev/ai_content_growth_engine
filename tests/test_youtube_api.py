from __future__ import annotations

from typing import Any

import pytest

from growth_engine.rate_limit import RateLimiter
from growth_engine.youtube.api import (
    ApiFailure,
    GoogleYouTubeAnalyticsReader,
    GoogleYouTubeDataReader,
    execute_with_retry,
)


class Response:
    def __init__(self, status: int) -> None:
        self.status = status


class HttpLikeError(Exception):
    def __init__(self, status: int) -> None:
        self.resp = Response(status)


def test_retry_is_bounded_for_transient_failures() -> None:
    attempts = 0
    delays: list[float] = []

    def operation() -> str:
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise HttpLikeError(503)
        return "ok"

    assert (
        execute_with_retry(
            operation,
            max_attempts=4,
            sleeper=delays.append,
            random_value=lambda: 0,
        )
        == "ok"
    )
    assert attempts == 3
    assert delays == [0.5, 1.0]


def test_quota_and_invalid_requests_are_not_retried() -> None:
    attempts = 0

    def operation() -> None:
        nonlocal attempts
        attempts += 1
        raise HttpLikeError(403)

    with pytest.raises(ApiFailure) as caught:
        execute_with_retry(operation, sleeper=lambda _: None)
    assert caught.value.category == "forbidden"
    assert attempts == 1


def test_google_quota_reason_is_classified_without_exposing_content() -> None:
    class QuotaError(HttpLikeError):
        content = (
            b'{"error":{"errors":[{"reason":"quotaExceeded",'
            b'"message":"sensitive provider detail"}]}}'
        )

    with pytest.raises(ApiFailure) as caught:
        execute_with_retry(lambda: (_ for _ in ()).throw(QuotaError(403)))
    assert caught.value.category == "quota_or_rate_limited"
    assert "sensitive provider detail" not in str(caught.value)


class Request:
    def __init__(self, response: dict[str, Any]) -> None:
        self.response = response

    def execute(self) -> dict[str, Any]:
        return self.response


class PlaylistResource:
    def __init__(self) -> None:
        self.tokens: list[str | None] = []

    def list(self, **kwargs: object) -> Request:
        token = kwargs.get("pageToken")
        self.tokens.append(token if isinstance(token, str) else None)
        if token == "next":
            return Request({"items": [{"contentDetails": {"videoId": "c"}}]})
        return Request(
            {
                "nextPageToken": "next",
                "items": [
                    {"contentDetails": {"videoId": "a"}},
                    {"contentDetails": {"videoId": "b"}},
                ],
            }
        )


class VideosResource:
    def __init__(self) -> None:
        self.batches: list[list[str]] = []

    def list(self, **kwargs: object) -> Request:
        ids = str(kwargs["id"]).split(",")
        self.batches.append(ids)
        return Request({"items": [{"id": item} for item in ids]})


class Service:
    def __init__(self) -> None:
        self.playlists = PlaylistResource()
        self.video_resource = VideosResource()

    def playlistItems(self) -> PlaylistResource:
        return self.playlists

    def videos(self) -> VideosResource:
        return self.video_resource


def test_pagination_and_video_batching() -> None:
    reader = object.__new__(GoogleYouTubeDataReader)
    service = Service()
    reader._service = service
    reader._limiter = RateLimiter({"youtube_data": 20})
    reader._audit_callback = None
    playlist = reader.list_upload_video_ids("uploads", 3)
    assert playlist["video_ids"] == ["a", "b", "c"]
    assert service.playlists.tokens == [None, "next"]

    ids = [f"video-{index}" for index in range(105)]
    response = reader.get_videos(ids)
    assert len(response["items"]) == 105
    assert [len(batch) for batch in service.video_resource.batches] == [50, 50, 5]


def test_repeated_pagination_token_is_rejected() -> None:
    class RepeatingPlaylist:
        def list(self, **kwargs: object) -> Request:
            return Request({"nextPageToken": "same", "items": []})

    class RepeatingService:
        def playlistItems(self) -> RepeatingPlaylist:
            return RepeatingPlaylist()

    reader = object.__new__(GoogleYouTubeDataReader)
    reader._service = RepeatingService()
    reader._limiter = RateLimiter({"youtube_data": 20})
    reader._audit_callback = None
    with pytest.raises(ApiFailure, match="repeated a page token"):
        reader.list_upload_video_ids("uploads", 3)


def test_analytics_rows_are_paginated_with_a_bound() -> None:
    headers = [{"name": "video"}, {"name": "views"}]

    class Reports:
        def __init__(self) -> None:
            self.start_indices: list[int] = []

        def query(self, **kwargs: object) -> Request:
            start_index = int(kwargs["startIndex"])
            self.start_indices.append(start_index)
            if start_index == 1:
                rows = [[f"video-{index}", index] for index in range(200)]
            else:
                rows = [["video-200", 200]]
            return Request({"columnHeaders": headers, "rows": rows})

    class AnalyticsService:
        def __init__(self) -> None:
            self.resource = Reports()

        def reports(self) -> Reports:
            return self.resource

    reader = object.__new__(GoogleYouTubeAnalyticsReader)
    service = AnalyticsService()
    reader._service = service
    reader._limiter = RateLimiter({"youtube_analytics": 10})
    reader._audit_callback = None
    response = reader.query_video_metrics("2026-01-01", "2026-01-31", ("views",))
    assert len(response["rows"]) == 201
    assert response["pagination"] == {"row_count": 201, "page_size": 200}
    assert service.resource.start_indices == [1, 201]
