"""Narrow official Google OAuth and YouTube API boundaries."""

from __future__ import annotations

import json
import os
import random
import time
from collections.abc import Callable, Sequence
from contextlib import suppress
from pathlib import Path
from typing import Any, Protocol, TypeVar, cast

from growth_engine.rate_limit import RateLimiter
from growth_engine.storage import write_text_atomic
from growth_engine.youtube import READ_ONLY_SCOPES

T = TypeVar("T")
AuditCallback = Callable[[str, dict[str, Any]], None]


class ApiFailure(RuntimeError):
    """Sanitized, classified official API failure."""

    def __init__(self, category: str, status_code: int | None, message: str) -> None:
        super().__init__(message)
        self.category = category
        self.status_code = status_code


def classify_http_status(status: int | None) -> tuple[str, bool]:
    if status == 401:
        return "invalid_or_revoked_authorization", False
    if status == 403:
        return "forbidden", False
    if status == 404:
        return "not_found", False
    if status == 429:
        return "quota_or_rate_limited", True
    if status is not None and status >= 500:
        return "api_unavailable", True
    return "invalid_request", False


def _status_from_exception(exc: Exception) -> int | None:
    response = getattr(exc, "resp", None)
    status = getattr(response, "status", None)
    return int(status) if isinstance(status, int) else None


def _google_error_reasons(exc: Exception) -> set[str]:
    reasons: set[str] = set()
    details = getattr(exc, "error_details", None)
    if isinstance(details, list):
        for detail in details:
            if isinstance(detail, dict) and isinstance(detail.get("reason"), str):
                reasons.add(detail["reason"])
    content = getattr(exc, "content", None)
    if isinstance(content, bytes):
        try:
            decoded = json.loads(content.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            decoded = None
        if isinstance(decoded, dict):
            error = decoded.get("error")
            errors = error.get("errors", []) if isinstance(error, dict) else []
            for detail in errors:
                if isinstance(detail, dict) and isinstance(detail.get("reason"), str):
                    reasons.add(detail["reason"])
    return reasons


def _classify_exception(exc: Exception) -> tuple[str, int | None, bool]:
    status = _status_from_exception(exc)
    category, retryable = classify_http_status(status)
    quota_reasons = {
        "quotaExceeded",
        "dailyLimitExceeded",
        "rateLimitExceeded",
        "userRateLimitExceeded",
    }
    if status == 403 and _google_error_reasons(exc) & quota_reasons:
        return "quota_or_rate_limited", status, False
    return category, status, retryable


def execute_with_retry(
    operation: Callable[[], T],
    *,
    max_attempts: int = 4,
    sleeper: Callable[[float], None] = time.sleep,
    random_value: Callable[[], float] = random.random,
) -> T:
    """Execute a request with bounded backoff for 429 and 5xx responses."""
    for attempt in range(max_attempts):
        try:
            return operation()
        except Exception as exc:
            category, status, retryable = _classify_exception(exc)
            if not retryable or attempt == max_attempts - 1:
                raise ApiFailure(
                    category,
                    status,
                    "Official YouTube API request failed "
                    f"({category}, HTTP {status or 'unknown'}).",
                ) from exc
            sleeper(min(8.0, 0.5 * (2**attempt)) + random_value() * 0.25)
    raise AssertionError("bounded retry loop exhausted unexpectedly")


class OAuthProvider(Protocol):
    def authorize(self, channel_config: dict[str, Any]) -> str: ...

    def credential_status(self, channel_config: dict[str, Any]) -> str: ...

    def load_credentials(self, channel_config: dict[str, Any]) -> object: ...


class YouTubeDataReader(Protocol):
    def get_owned_channel(self) -> dict[str, Any]: ...

    def list_upload_video_ids(
        self, uploads_playlist_id: str, max_items: int
    ) -> dict[str, Any]: ...

    def get_videos(self, video_ids: Sequence[str]) -> dict[str, Any]: ...


class YouTubeAnalyticsReader(Protocol):
    def query_video_metrics(
        self, start_date: str, end_date: str, metrics: Sequence[str]
    ) -> dict[str, Any]: ...


class GoogleOAuthProvider:
    """Official installed-application OAuth with local untracked token storage."""

    def authorize(self, channel_config: dict[str, Any]) -> str:
        try:
            from google_auth_oauthlib.flow import InstalledAppFlow  # type: ignore[import-not-found]
        except ImportError as exc:
            raise RuntimeError(
                "Google OAuth libraries are unavailable; install the project dependencies."
            ) from exc
        client_path = _configured_path(
            channel_config, "client_secrets_path", "client_secrets_path_env"
        )
        try:
            flow = InstalledAppFlow.from_client_secrets_file(
                str(client_path), scopes=list(READ_ONLY_SCOPES)
            )
            credentials = flow.run_local_server(port=0, open_browser=True)
        except Exception as exc:
            raise RuntimeError(
                "Google OAuth authorization could not start or was denied. "
                "Verify browser access and the installed-app client configuration."
            ) from exc
        token_path = _configured_path(channel_config, "token_path", "token_path_env")
        write_text_atomic(token_path, credentials.to_json())
        with suppress(OSError):
            token_path.chmod(0o600)
        return "authorized"

    def credential_status(self, channel_config: dict[str, Any]) -> str:
        token_path = _configured_path(channel_config, "token_path", "token_path_env")
        if not token_path.is_file():
            return "configured_not_authorized"
        try:
            credentials = self.load_credentials(channel_config)
            valid = bool(getattr(credentials, "valid", False))
            expired = bool(getattr(credentials, "expired", False))
            refresh_token = getattr(credentials, "refresh_token", None)
            if valid:
                return "authorized"
            if expired and not refresh_token:
                return "token_refresh_required"
            if expired and refresh_token:
                from google.auth.transport.requests import Request  # type: ignore[import-not-found]

                cast(Any, credentials).refresh(Request())
                write_text_atomic(token_path, cast(Any, credentials).to_json())
                return "authorized"
            return "invalid_or_revoked_authorization"
        except Exception:
            return "invalid_or_revoked_authorization"

    def load_credentials(self, channel_config: dict[str, Any]) -> object:
        try:
            from google.oauth2.credentials import Credentials  # type: ignore[import-not-found]
        except ImportError as exc:
            raise RuntimeError(
                "Google authentication libraries are unavailable; install dependencies."
            ) from exc
        token_path = _configured_path(channel_config, "token_path", "token_path_env")
        return Credentials.from_authorized_user_file(str(token_path), list(READ_ONLY_SCOPES))


def _configured_path(
    channel_config: dict[str, Any], path_key: str, environment_key: str
) -> Path:
    variable = channel_config.get(environment_key)
    override = os.environ.get(variable) if isinstance(variable, str) else None
    return Path(override or str(channel_config[path_key])).expanduser().resolve()


def _build_google_service(api: str, version: str, credentials: object) -> Any:
    try:
        import httplib2  # type: ignore[import-untyped]
        from google_auth_httplib2 import AuthorizedHttp  # type: ignore[import-not-found]
        from googleapiclient.discovery import build  # type: ignore[import-not-found]
    except ImportError as exc:
        raise RuntimeError(
            "Google API client library is unavailable; install dependencies."
        ) from exc
    authorized_http = AuthorizedHttp(
        credentials, http=httplib2.Http(timeout=30)
    )
    return build(api, version, http=authorized_http, cache_discovery=False)


class GoogleYouTubeDataReader:
    """Official YouTube Data API v3 reader with uploads-playlist pagination."""

    def __init__(
        self,
        credentials: object,
        limiter: RateLimiter | None = None,
        audit_callback: AuditCallback | None = None,
    ) -> None:
        self._service = _build_google_service("youtube", "v3", credentials)
        self._limiter = limiter or RateLimiter({"youtube_data": 120})
        self._audit_callback = audit_callback

    def _permit(self) -> None:
        if not self._limiter.allow("youtube_data"):
            raise ApiFailure(
                "local_rate_limit", 429, "Local YouTube Data API rate limit reached."
            )

    def _execute(self, method: str, operation: Callable[[], Any]) -> Any:
        try:
            response = execute_with_retry(operation)
        except ApiFailure as exc:
            if self._audit_callback:
                self._audit_callback(
                    "youtube_api_request",
                    {
                        "api": "youtube_data_v3",
                        "method": method,
                        "outcome": exc.category,
                        "http_status": exc.status_code,
                    },
                )
            raise
        if self._audit_callback:
            self._audit_callback(
                "youtube_api_request",
                {
                    "api": "youtube_data_v3",
                    "method": method,
                    "outcome": "succeeded",
                },
            )
        return response

    def get_owned_channel(self) -> dict[str, Any]:
        self._permit()
        request = self._service.channels().list(
            part="id,snippet,statistics,contentDetails,status", mine=True
        )
        response = self._execute("channels.list", request.execute)
        if not isinstance(response, dict):
            raise ApiFailure("malformed_response", None, "Channel response was not a JSON object.")
        return cast(dict[str, Any], response)

    def list_upload_video_ids(
        self, uploads_playlist_id: str, max_items: int
    ) -> dict[str, Any]:
        pages: list[dict[str, Any]] = []
        ids: list[str] = []
        page_token: str | None = None
        seen_tokens: set[str] = set()
        while len(ids) < max_items:
            if len(pages) >= 100:
                raise ApiFailure(
                    "malformed_response",
                    None,
                    "Uploads-playlist pagination exceeded the bounded page limit.",
                )
            self._permit()
            request = self._service.playlistItems().list(
                part="contentDetails,status",
                playlistId=uploads_playlist_id,
                maxResults=min(50, max_items - len(ids)),
                pageToken=page_token,
            )
            page = self._execute("playlistItems.list", request.execute)
            if not isinstance(page, dict):
                raise ApiFailure(
                    "malformed_response", None, "Playlist response was not a JSON object."
                )
            pages.append(cast(dict[str, Any], page))
            for item in page.get("items", []):
                if isinstance(item, dict):
                    details = item.get("contentDetails", {})
                    video_id = details.get("videoId") if isinstance(details, dict) else None
                    if isinstance(video_id, str) and video_id not in ids:
                        ids.append(video_id)
                        if len(ids) == max_items:
                            break
            token = page.get("nextPageToken")
            page_token = token if isinstance(token, str) and token else None
            if page_token is None:
                break
            if page_token in seen_tokens:
                raise ApiFailure(
                    "malformed_response",
                    None,
                    "Uploads-playlist pagination repeated a page token.",
                )
            seen_tokens.add(page_token)
        return {"pages": pages, "video_ids": ids}

    def get_videos(self, video_ids: Sequence[str]) -> dict[str, Any]:
        batches: list[dict[str, Any]] = []
        items: list[dict[str, Any]] = []
        for start in range(0, len(video_ids), 50):
            self._permit()
            batch = list(video_ids[start : start + 50])
            request = self._service.videos().list(
                part="id,snippet,contentDetails,status,statistics",
                id=",".join(batch),
                maxResults=50,
            )
            response = self._execute("videos.list", request.execute)
            if not isinstance(response, dict):
                raise ApiFailure(
                    "malformed_response", None, "Videos response was not a JSON object."
                )
            batches.append(cast(dict[str, Any], response))
            items.extend(item for item in response.get("items", []) if isinstance(item, dict))
        return {"batches": batches, "items": items}


class GoogleYouTubeAnalyticsReader:
    """Official YouTube Analytics API reader."""

    def __init__(
        self,
        credentials: object,
        limiter: RateLimiter | None = None,
        audit_callback: AuditCallback | None = None,
    ) -> None:
        self._service = _build_google_service(
            "youtubeAnalytics", "v2", credentials
        )
        self._limiter = limiter or RateLimiter({"youtube_analytics": 60})
        self._audit_callback = audit_callback

    def query_video_metrics(
        self, start_date: str, end_date: str, metrics: Sequence[str]
    ) -> dict[str, Any]:
        combined: dict[str, Any] | None = None
        combined_rows: list[list[object]] = []
        start_index = 1
        for _page_number in range(100):
            if not self._limiter.allow("youtube_analytics"):
                raise ApiFailure(
                    "local_rate_limit",
                    429,
                    "Local YouTube Analytics API rate limit reached.",
                )
            request = self._service.reports().query(
                ids="channel==MINE",
                startDate=start_date,
                endDate=end_date,
                dimensions="video",
                metrics=",".join(metrics),
                sort="-views",
                maxResults=200,
                startIndex=start_index,
            )
            try:
                response = execute_with_retry(request.execute)
            except ApiFailure as exc:
                if self._audit_callback:
                    self._audit_callback(
                        "youtube_api_request",
                        {
                            "api": "youtube_analytics_v2",
                            "method": "reports.query",
                            "outcome": exc.category,
                            "http_status": exc.status_code,
                        },
                    )
                raise
            if self._audit_callback:
                self._audit_callback(
                    "youtube_api_request",
                    {
                        "api": "youtube_analytics_v2",
                        "method": "reports.query",
                        "outcome": "succeeded",
                    },
                )
            if not isinstance(response, dict):
                raise ApiFailure(
                    "malformed_response",
                    None,
                    "Analytics response was not a JSON object.",
                )
            if combined is None:
                combined = cast(dict[str, Any], response.copy())
            elif response.get("columnHeaders") != combined.get("columnHeaders"):
                raise ApiFailure(
                    "malformed_response",
                    None,
                    "Analytics pagination returned incompatible column headers.",
                )
            rows = response.get("rows", []) or []
            if not isinstance(rows, list) or not all(isinstance(row, list) for row in rows):
                raise ApiFailure(
                    "malformed_response", None, "Analytics rows were malformed."
                )
            combined_rows.extend(cast(list[list[object]], rows))
            if len(rows) < 200:
                break
            start_index += len(rows)
        else:
            raise ApiFailure(
                "malformed_response",
                None,
                "Analytics pagination exceeded the bounded page limit.",
            )
        if combined is None:
            raise ApiFailure(
                "malformed_response", None, "Analytics response was unavailable."
            )
        combined["rows"] = combined_rows
        combined["pagination"] = {"row_count": len(combined_rows), "page_size": 200}
        return combined


def load_google_readers(
    channel_config: dict[str, Any],
    oauth_provider: OAuthProvider | None = None,
    audit_callback: AuditCallback | None = None,
) -> tuple[YouTubeDataReader, YouTubeAnalyticsReader]:
    provider = oauth_provider or GoogleOAuthProvider()
    credentials = provider.load_credentials(channel_config)
    return (
        GoogleYouTubeDataReader(credentials, audit_callback=audit_callback),
        GoogleYouTubeAnalyticsReader(credentials, audit_callback=audit_callback),
    )
