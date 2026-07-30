from __future__ import annotations

import json
from pathlib import Path

import pytest

from growth_engine.youtube.normalize import (
    normalize_analytics_rows,
    normalize_channel,
    normalize_videos,
    parse_iso8601_duration,
)

FIXTURES = Path(__file__).parent / "fixtures" / "youtube"


def load_fixture(name: str) -> dict[str, object]:
    value = json.loads((FIXTURES / name).read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("PT8M30S", 510),
        ("PT1H2M3S", 3723),
        ("P1DT1S", 86401),
        ("PT0S", 0),
        ("invalid", None),
        (None, None),
    ],
)
def test_iso8601_duration(raw: object, expected: int | None) -> None:
    assert parse_iso8601_duration(raw) == expected


def test_channel_and_video_normalization_preserve_missing_values() -> None:
    channel = normalize_channel(
        load_fixture("channel.json"),
        raw_snapshot_id="raw-channel",
        normalized_at="2026-01-31T00:00:00+00:00",
    )
    assert channel["channel_id"] == "UC_fixture_owner"
    assert channel["uploads_playlist_id"] == "UU_fixture_uploads"
    assert channel["subscriber_count"] == 4200

    videos = normalize_videos(
        ["video-a", "video-b", "video-c"],
        load_fixture("videos.json"),
        channel_id="UC_fixture_owner",
        raw_snapshot_id="raw-videos",
        normalized_at="2026-01-31T00:00:00+00:00",
    )
    assert videos[0]["duration_seconds"] == 510
    assert videos[1]["like_count"] is None
    assert videos[1]["comment_count"] is None
    assert videos[2]["availability"] == "unavailable_or_deleted"
    assert videos[2]["view_count"] is None


def test_analytics_normalization_supports_empty_and_partial_tables() -> None:
    empty, empty_headers = normalize_analytics_rows(
        load_fixture("analytics_empty.json"),
        channel_id="UC_fixture_owner",
        start_date="2026-01-01",
        end_date="2026-01-31",
        raw_snapshot_id="empty",
        normalized_at="2026-02-01T00:00:00+00:00",
    )
    assert empty == []
    assert empty_headers == ["video", "views"]

    partial, headers = normalize_analytics_rows(
        load_fixture("analytics_partial.json"),
        channel_id="UC_fixture_owner",
        start_date="2026-01-01",
        end_date="2026-01-31",
        raw_snapshot_id="partial",
        normalized_at="2026-02-01T00:00:00+00:00",
    )
    assert headers == ["video", "views", "likes"]
    assert partial[0]["metrics"] == {"views": 10, "likes": 1}


def test_malformed_analytics_row_is_rejected() -> None:
    malformed = {
        "columnHeaders": [{"name": "video"}, {"name": "views"}],
        "rows": [["video-a"]],
    }
    with pytest.raises(ValueError, match="row length"):
        normalize_analytics_rows(
            malformed,
            channel_id="UC_fixture_owner",
            start_date="2026-01-01",
            end_date="2026-01-31",
            raw_snapshot_id="malformed",
            normalized_at="2026-02-01T00:00:00+00:00",
        )
