from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path
from typing import Any

import pytest

import growth_engine.youtube.sync as youtube_sync_module
from growth_engine.cli import run
from growth_engine.policy import Action, PolicyViolation, authorize
from growth_engine.storage import Workspace, redact
from growth_engine.youtube.api import ApiFailure
from growth_engine.youtube.config import (
    configure_channel,
    get_channel_config,
    load_config,
)
from growth_engine.youtube.intelligence import (
    RULE_VERSION,
    analyze_performance,
    diagnose_content,
    diagnose_video,
    safe_rate,
)
from growth_engine.youtube.reporting import generate_youtube_report
from growth_engine.youtube.sync import (
    authorize_youtube,
    sync_all,
    sync_analytics,
    sync_channel,
    sync_videos,
    youtube_status,
)

FIXTURES = Path(__file__).parent / "fixtures" / "youtube"
START = "2026-01-01"
END = "2026-01-31"


def fixture(name: str) -> dict[str, Any]:
    value = json.loads((FIXTURES / name).read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


class FakeOAuth:
    def __init__(self, status: str = "authorized") -> None:
        self.status = status
        self.authorize_calls = 0

    def authorize(self, channel_config: dict[str, Any]) -> str:
        self.authorize_calls += 1
        return "authorized"

    def credential_status(self, channel_config: dict[str, Any]) -> str:
        return self.status

    def load_credentials(self, channel_config: dict[str, Any]) -> object:
        return object()


class FakeDataReader:
    def __init__(self) -> None:
        self.channel_calls = 0
        self.playlist_calls = 0
        self.video_calls = 0

    def get_owned_channel(self) -> dict[str, Any]:
        self.channel_calls += 1
        return fixture("channel.json")

    def list_upload_video_ids(
        self, uploads_playlist_id: str, max_items: int
    ) -> dict[str, Any]:
        self.playlist_calls += 1
        assert uploads_playlist_id == "UU_fixture_uploads"
        pages = [fixture("playlist_page_1.json"), fixture("playlist_page_2.json")]
        return {"pages": pages, "video_ids": ["video-a", "video-b", "video-c"][:max_items]}

    def get_videos(self, video_ids: list[str]) -> dict[str, Any]:
        self.video_calls += 1
        response = fixture("videos.json")
        return {"batches": [response], "items": response["items"]}


class FakeAnalyticsReader:
    def __init__(self) -> None:
        self.calls = 0

    def query_video_metrics(
        self, start_date: str, end_date: str, metrics: tuple[str, ...]
    ) -> dict[str, Any]:
        self.calls += 1
        assert (start_date, end_date) == (START, END)
        assert "views" in metrics
        return fixture("analytics.json")


def initialized_workspace(tmp_path: Path) -> Workspace:
    workspace = Workspace(tmp_path)
    workspace.initialize("Fixture Creator", "art")
    configure_channel(workspace, "art-forever", FIXTURES / "client_secrets.json")
    return workspace


def hashes(path: Path) -> dict[str, str]:
    return {
        str(item.relative_to(path)): hashlib.sha256(item.read_bytes()).hexdigest()
        for item in path.rglob("*")
        if item.is_file()
    }


def test_full_offline_pipeline_is_normalized_idempotent_and_raw_immutable(
    tmp_path: Path,
) -> None:
    workspace = initialized_workspace(tmp_path)
    oauth = FakeOAuth()
    data = FakeDataReader()
    analytics = FakeAnalyticsReader()
    channel_result = sync_channel(
        workspace, "art-forever", provider=oauth, data_reader=data
    )
    assert channel_result["state"] == "succeeded"
    config_text = (workspace.data_dir / "youtube/config.json").read_text(
        encoding="utf-8"
    )
    audit_text = (workspace.data_dir / "logs/audit.jsonl").read_text(encoding="utf-8")
    assert "fixture-secret-never-copy" not in config_text
    assert "fixture-secret-never-copy" not in audit_text
    assert "[REDACTED]" in audit_text
    first_videos = sync_videos(
        workspace, "art-forever", 100, provider=oauth, data_reader=data
    )
    assert first_videos["unavailable_count"] == 1
    first_analytics = sync_analytics(
        workspace,
        "art-forever",
        START,
        END,
        provider=oauth,
        analytics_reader=analytics,
    )
    assert first_analytics["row_count"] == 3

    raw_root = workspace.data_dir / "raw" / "youtube" / "art-forever"
    first_raw = hashes(raw_root)
    assert len(first_raw) == 4
    sync_videos(workspace, "art-forever", 100, provider=oauth, data_reader=data)
    sync_analytics(
        workspace,
        "art-forever",
        START,
        END,
        provider=oauth,
        analytics_reader=analytics,
    )
    second_raw = hashes(raw_root)
    assert all(second_raw[name] == digest for name, digest in first_raw.items())
    assert len(second_raw) == 7

    videos = workspace.read_json(
        workspace.data_dir / "normalized/youtube/art-forever/videos.json"
    )
    rows = workspace.read_json(
        workspace.data_dir / "normalized/youtube/art-forever/analytics_rows.json"
    )
    assert len(videos["records"]) == 3
    assert len(rows["records"]) == 3

    analysis = analyze_performance(workspace, "art-forever", START, END)
    assert analysis["network_requests"] == 0
    assert safe_rate(5, 100) == (5.0, None)
    zero_rate, reason = safe_rate(1, 0)
    assert zero_rate is None and reason == "zero_denominator"
    summary = {
        item["name"]: item
        for item in analysis["metrics"]
        if item["video_id"] is None
    }
    assert summary["views_per_video"]["value"] == 3400.0
    assert summary["publishing_frequency"]["value"] == pytest.approx(0.4516)
    assert all("formula_version" in item for item in analysis["metrics"])
    diagnoses = diagnose_content(workspace, "art-forever", START, END)
    assert diagnoses["rule_version"] == RULE_VERSION
    assert all(item["evidence"] for item in diagnoses["diagnoses"])
    report = generate_youtube_report(workspace, "art-forever", START, END)
    assert Path(report["json_path"]).is_file()
    assert Path(report["markdown_path"]).is_file()
    assert report["raw_api_facts"]["raw_snapshot_count"] == 7


@pytest.mark.parametrize(
    ("metrics", "expected"),
    [
        ({"views": 10.0}, "insufficient_data"),
        (
            {
                "views": 100.0,
                "impressions": 400.0,
                "impression_click_through_rate": 4.0,
                "average_percentage_viewed": 60.0,
            },
            "low_impressions",
        ),
        (
            {
                "views": 100.0,
                "impressions": 800.0,
                "impression_click_through_rate": 1.0,
                "average_percentage_viewed": 50.0,
            },
            "weak_click_through_rate",
        ),
        (
            {
                "views": 100.0,
                "impressions": 1500.0,
                "impression_click_through_rate": 4.0,
                "average_percentage_viewed": 30.0,
            },
            "strong_packaging_low_retention",
        ),
        (
            {
                "views": 100.0,
                "impressions": 800.0,
                "impression_click_through_rate": 4.0,
                "average_percentage_viewed": 60.0,
            },
            "strong_retention_low_distribution",
        ),
        (
            {
                "views": 100.0,
                "impressions": 1500.0,
                "impression_click_through_rate": None,
                "average_percentage_viewed": 20.0,
            },
            "weak_opening_or_retention",
        ),
        (
            {
                "views": 100.0,
                "impressions": 800.0,
                "impression_click_through_rate": 3.0,
                "average_percentage_viewed": None,
                "engagement_rate": 5.0,
            },
            "high_engagement_low_reach",
        ),
        (
            {
                "views": 100.0,
                "impressions": 1500.0,
                "impression_click_through_rate": 4.0,
                "average_percentage_viewed": 60.0,
                "subscriber_conversion_rate": 0.1,
            },
            "high_reach_low_subscriber_conversion",
        ),
        (
            {
                "views": 100.0,
                "impressions": 1500.0,
                "impression_click_through_rate": 4.0,
                "average_percentage_viewed": 60.0,
                "subscriber_conversion_rate": 1.0,
            },
            "strong_overall_performance",
        ),
        (
            {
                "views": 100.0,
                "impressions": 1500.0,
                "impression_click_through_rate": 1.8,
                "average_percentage_viewed": 50.0,
                "subscriber_conversion_rate": 1.0,
                "engagement_rate": 1.0,
            },
            "inconclusive_mixed_signals",
        ),
    ],
)
def test_diagnosis_rules(metrics: dict[str, float | None], expected: str) -> None:
    result = diagnose_video("video", metrics, {}, ["source"])
    assert result["diagnosis"] == expected
    assert result["rule_version"] == RULE_VERSION


def test_dry_run_causes_no_mutation_or_network(tmp_path: Path) -> None:
    workspace = initialized_workspace(tmp_path)
    before = hashes(workspace.data_dir)
    data = FakeDataReader()
    result = sync_channel(
        workspace,
        "art-forever",
        provider=FakeOAuth(),
        data_reader=data,
        dry_run=True,
    )
    assert result["state"] == "skipped"
    assert data.channel_calls == 0
    assert hashes(workspace.data_dir) == before


def test_configure_dry_run_and_malformed_client_validation(tmp_path: Path) -> None:
    workspace = Workspace(tmp_path)
    workspace.initialize("Fixture", "art")
    result = configure_channel(
        workspace,
        "dry-channel",
        FIXTURES / "client_secrets.json",
        dry_run=True,
    )
    assert result["status"] == "configured"
    assert get_channel_config(workspace, "dry-channel") is None
    malformed = tmp_path / "malformed-client.json"
    malformed.write_text('{"web": {}}', encoding="utf-8")
    with pytest.raises(ValueError, match="'installed' client"):
        configure_channel(workspace, "bad", malformed)


@pytest.mark.parametrize(
    "status",
    [
        "configured_not_authorized",
        "token_refresh_required",
        "invalid_or_revoked_authorization",
    ],
)
def test_status_exposes_authorization_states(tmp_path: Path, status: str) -> None:
    workspace = initialized_workspace(tmp_path)
    assert youtube_status(workspace, "art-forever", provider=FakeOAuth(status))[
        "status"
    ] == status


def test_not_configured_authorization_failure_and_refreshable_status(
    tmp_path: Path,
) -> None:
    workspace = Workspace(tmp_path)
    workspace.initialize("Fixture", "art")
    assert youtube_status(workspace, "missing")["status"] == "not_configured"
    configure_channel(workspace, "art-forever", FIXTURES / "client_secrets.json")

    class DeniedOAuth(FakeOAuth):
        def authorize(self, channel_config: dict[str, Any]) -> str:
            raise RuntimeError("sanitized authorization denial")

    with pytest.raises(RuntimeError, match="authorization denial"):
        authorize_youtube(workspace, "art-forever", provider=DeniedOAuth())

    class RefreshableOAuth(FakeOAuth):
        refreshed = False

        def credential_status(self, channel_config: dict[str, Any]) -> str:
            self.refreshed = True
            return "authorized"

    refreshable = RefreshableOAuth()
    assert youtube_status(
        workspace,
        "art-forever",
        provider=refreshable,
        data_reader=FakeDataReader(),
    )["status"] == "authorized"
    assert refreshable.refreshed


def test_status_detects_identity_mismatch(tmp_path: Path) -> None:
    workspace = initialized_workspace(tmp_path)
    config = get_channel_config(workspace, "art-forever")
    assert config is not None
    full = load_config(workspace)
    full["channels"]["art-forever"]["expected_channel_id"] = "UC_other"
    workspace.write_json("youtube/config.json", full)
    result = youtube_status(
        workspace,
        "art-forever",
        provider=FakeOAuth(),
        data_reader=FakeDataReader(),
    )
    assert result["status"] == "channel_identity_mismatch"


def test_status_authorized_and_api_unavailable(tmp_path: Path) -> None:
    workspace = initialized_workspace(tmp_path)
    authorized = youtube_status(
        workspace,
        "art-forever",
        provider=FakeOAuth(),
        data_reader=FakeDataReader(),
    )
    assert authorized["status"] == "authorized"

    class UnavailableReader(FakeDataReader):
        def get_owned_channel(self) -> dict[str, Any]:
            raise ApiFailure("api_unavailable", 503, "sanitized unavailable")

    unavailable = youtube_status(
        workspace,
        "art-forever",
        provider=FakeOAuth(),
        data_reader=UnavailableReader(),
    )
    assert unavailable["status"] == "api_unavailable"


def test_quota_status_and_api_failure_are_sanitized(tmp_path: Path) -> None:
    workspace = initialized_workspace(tmp_path)

    class QuotaReader(FakeDataReader):
        def get_owned_channel(self) -> dict[str, Any]:
            raise ApiFailure("quota_or_rate_limited", 403, "sanitized quota failure")

    result = youtube_status(
        workspace,
        "art-forever",
        provider=FakeOAuth(),
        data_reader=QuotaReader(),
    )
    assert result == {
        "channel": "art-forever",
        "status": "quota_or_rate_limit_response",
        "http_status": 403,
    }


def test_partial_analytics_metrics_are_preserved_deterministically(
    tmp_path: Path,
) -> None:
    workspace = initialized_workspace(tmp_path)
    sync_channel(
        workspace,
        "art-forever",
        provider=FakeOAuth(),
        data_reader=FakeDataReader(),
    )

    class PartialReader:
        def query_video_metrics(
            self, start_date: str, end_date: str, metrics: tuple[str, ...]
        ) -> dict[str, Any]:
            if len(metrics) > 1 or metrics[0] != "views":
                raise ApiFailure("invalid_request", 400, "unsupported metric")
            return fixture("analytics_empty.json")

    result = sync_analytics(
        workspace,
        "art-forever",
        START,
        END,
        provider=FakeOAuth(),
        analytics_reader=PartialReader(),
    )
    assert result["api_metric_names"] == ["views"]
    assert "likes" in result["unsupported_metrics"]
    assert result["row_count"] == 0


def test_analysis_rejects_incompatible_date_range(tmp_path: Path) -> None:
    workspace = initialized_workspace(tmp_path)
    sync_channel(
        workspace,
        "art-forever",
        provider=FakeOAuth(),
        data_reader=FakeDataReader(),
    )
    sync_analytics(
        workspace,
        "art-forever",
        START,
        END,
        provider=FakeOAuth(),
        analytics_reader=FakeAnalyticsReader(),
    )
    with pytest.raises(ValueError, match="exact requested"):
        analyze_performance(workspace, "art-forever", "2026-01-02", END)


def test_full_sync_reports_partial_failure(tmp_path: Path) -> None:
    workspace = initialized_workspace(tmp_path)

    class VideosFail(FakeDataReader):
        def list_upload_video_ids(
            self, uploads_playlist_id: str, max_items: int
        ) -> dict[str, Any]:
            raise ApiFailure("api_unavailable", 503, "sanitized unavailable")

    result = sync_all(
        workspace,
        "art-forever",
        START,
        END,
        100,
        provider=FakeOAuth(),
        data_reader=VideosFail(),
        analytics_reader=FakeAnalyticsReader(),
    )
    assert result["state"] == "partially_succeeded"
    assert [step["state"] for step in result["steps"]] == ["succeeded", "failed"]


def test_redaction_and_new_write_policy_actions() -> None:
    redacted = redact(
        {
            "access_token": "access",
            "refreshToken": "refresh",
            "client_secret": "secret",
            "nested": {"password": "password", "safe": "visible"},
        }
    )
    assert isinstance(redacted, dict)
    assert redacted["access_token"] == "[REDACTED]"
    assert redacted["nested"] == {"password": "[REDACTED]", "safe": "visible"}
    for action in (
        Action.PLATFORM_WRITE,
        Action.VIDEO_UPLOAD,
        Action.METADATA_UPDATE,
        Action.COMMENT_WRITE,
        Action.RATING_WRITE,
        Action.SUBSCRIPTION_WRITE,
    ):
        with pytest.raises(PolicyViolation):
            authorize(action)


def test_cli_fixture_backed_full_sync_and_local_commands(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    oauth = FakeOAuth()
    data = FakeDataReader()
    analytics = FakeAnalyticsReader()
    monkeypatch.setattr(youtube_sync_module, "GoogleOAuthProvider", lambda: oauth)
    monkeypatch.setattr(
        youtube_sync_module,
        "load_google_readers",
        lambda config, provider=None, audit_callback=None: (data, analytics),
    )
    workspace_args = ["--workspace", str(tmp_path), "--json"]
    assert run(["init", *workspace_args]) == 0
    capsys.readouterr()
    assert (
        run(
            [
                "youtube",
                "configure",
                "--client-secrets",
                str(FIXTURES / "client_secrets.json"),
                "--channel-alias",
                "art-forever",
                *workspace_args,
            ]
        )
        == 0
    )
    capsys.readouterr()
    assert run(["youtube", "auth", "--channel", "art-forever", *workspace_args]) == 0
    capsys.readouterr()
    assert (
        run(
            [
                "youtube",
                "sync",
                "all",
                "--channel",
                "art-forever",
                "--start-date",
                START,
                "--end-date",
                END,
                "--max-items",
                "100",
                *workspace_args,
            ]
        )
        == 0
    )
    full = json.loads(capsys.readouterr().out)
    assert full["state"] == "succeeded"
    for command in (
        ["analytics", "analyze"],
        ["analytics", "diagnose"],
        ["report", "youtube"],
    ):
        assert (
            run(
                [
                    *command,
                    "--channel",
                    "art-forever",
                    "--start-date",
                    START,
                    "--end-date",
                    END,
                    *workspace_args,
                ]
            )
            == 0
        )
        parsed = json.loads(capsys.readouterr().out)
        assert parsed["state"] == "succeeded"


def test_readme_youtube_commands_execute_with_fixture_boundaries(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    oauth = FakeOAuth()
    data = FakeDataReader()
    analytics = FakeAnalyticsReader()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(youtube_sync_module, "GoogleOAuthProvider", lambda: oauth)
    monkeypatch.setattr(
        youtube_sync_module,
        "load_google_readers",
        lambda config, provider=None, audit_callback=None: (data, analytics),
    )
    commands = [
        ["init", "--creator", "Example Creator", "--niche", "sustainable design"],
        [
            "youtube",
            "configure",
            "--client-secrets",
            str(FIXTURES / "client_secrets.json"),
            "--channel-alias",
            "art-forever",
        ],
        ["youtube", "auth", "--channel", "art-forever"],
        ["youtube", "status", "--channel", "art-forever"],
        ["youtube", "sync", "channel", "--channel", "art-forever"],
        [
            "youtube",
            "sync",
            "videos",
            "--channel",
            "art-forever",
            "--max-items",
            "100",
        ],
        [
            "youtube",
            "sync",
            "analytics",
            "--channel",
            "art-forever",
            "--start-date",
            START,
            "--end-date",
            END,
        ],
        [
            "youtube",
            "sync",
            "all",
            "--channel",
            "art-forever",
            "--start-date",
            START,
            "--end-date",
            END,
            "--max-items",
            "100",
        ],
        [
            "analytics",
            "analyze",
            "--channel",
            "art-forever",
            "--start-date",
            START,
            "--end-date",
            END,
        ],
        [
            "analytics",
            "diagnose",
            "--channel",
            "art-forever",
            "--start-date",
            START,
            "--end-date",
            END,
        ],
        [
            "report",
            "youtube",
            "--channel",
            "art-forever",
            "--start-date",
            START,
            "--end-date",
            END,
        ],
    ]
    for command in commands:
        assert run(command) == 0
        assert capsys.readouterr().out

    workspace = Workspace(tmp_path)
    before = hashes(workspace.data_dir)
    assert sync_all(
        workspace,
        "art-forever",
        START,
        END,
        100,
        provider=oauth,
        data_reader=data,
        analytics_reader=analytics,
        dry_run=True,
    )["state"] == "skipped"
    analyze_performance(workspace, "art-forever", START, END, dry_run=True)
    diagnose_content(workspace, "art-forever", START, END, dry_run=True)
    generate_youtube_report(
        workspace, "art-forever", START, END, dry_run=True
    )
    assert hashes(workspace.data_dir) == before


def test_cli_missing_authorization_and_quota_return_nonzero(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    workspace = Workspace(tmp_path)
    workspace.initialize("Fixture", "art")
    configure_channel(workspace, "art-forever", FIXTURES / "client_secrets.json")
    monkeypatch.setattr(
        youtube_sync_module,
        "GoogleOAuthProvider",
        lambda: FakeOAuth("configured_not_authorized"),
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "growth-engine",
            "youtube",
            "sync",
            "channel",
            "--channel",
            "art-forever",
            "--workspace",
            str(tmp_path),
        ],
    )
    from growth_engine.cli import entrypoint

    with pytest.raises(SystemExit) as unauthorized:
        entrypoint()
    assert unauthorized.value.code == 2
    assert "not authorized" in capsys.readouterr().err

    class QuotaReader(FakeDataReader):
        def get_owned_channel(self) -> dict[str, Any]:
            raise ApiFailure("quota_or_rate_limited", 403, "sanitized quota failure")

    monkeypatch.setattr(
        youtube_sync_module, "GoogleOAuthProvider", lambda: FakeOAuth()
    )
    monkeypatch.setattr(
        youtube_sync_module,
        "load_google_readers",
        lambda config, provider=None, audit_callback=None: (
            QuotaReader(),
            FakeAnalyticsReader(),
        ),
    )
    with pytest.raises(SystemExit) as quota:
        entrypoint()
    assert quota.value.code == 2
    assert "sanitized quota failure" in capsys.readouterr().err
