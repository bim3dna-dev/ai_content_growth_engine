"""Versioned local YouTube channel configuration."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from growth_engine.storage import Workspace, WorkspaceError, utc_now

_ALIAS = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
_REQUIRED_CLIENT_FIELDS = frozenset(
    {"client_id", "client_secret", "auth_uri", "token_uri", "redirect_uris"}
)


def validate_alias(alias: str) -> str:
    clean = alias.strip()
    if not _ALIAS.fullmatch(clean):
        raise ValueError(
            "Channel alias must be 1-64 Windows-safe letters, numbers, hyphens, or underscores."
        )
    return clean


def validate_client_secrets(path: Path) -> None:
    if not path.is_file():
        raise ValueError(f"Google OAuth client-secrets file does not exist: '{path}'.")
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("Google OAuth client-secrets file is not valid JSON.") from exc
    if not isinstance(value, dict) or not isinstance(value.get("installed"), dict):
        raise ValueError("Google OAuth client-secrets file must contain an 'installed' client.")
    missing = _REQUIRED_CLIENT_FIELDS - value["installed"].keys()
    if missing:
        names = ", ".join(sorted(missing))
        raise ValueError(f"Google OAuth installed client is missing required fields: {names}.")


def config_path(workspace: Workspace) -> Path:
    return workspace.data_dir / "youtube" / "config.json"


def load_config(workspace: Workspace) -> dict[str, Any]:
    workspace.require_initialized()
    path = config_path(workspace)
    if not path.is_file():
        return {"schema_version": 1, "channels": {}}
    config = workspace.read_json(path)
    if config.get("schema_version") != 1 or not isinstance(config.get("channels"), dict):
        raise WorkspaceError("YouTube configuration has an unsupported or malformed schema.")
    return config


def get_channel_config(workspace: Workspace, alias: str) -> dict[str, Any] | None:
    config = load_config(workspace)
    value = config["channels"].get(validate_alias(alias))
    return value if isinstance(value, dict) else None


def configure_channel(
    workspace: Workspace,
    alias: str,
    client_secrets: Path,
    *,
    dry_run: bool = False,
) -> dict[str, Any]:
    workspace.require_initialized()
    clean_alias = validate_alias(alias)
    resolved = client_secrets.expanduser().resolve()
    validate_client_secrets(resolved)
    config = load_config(workspace)
    previous = config["channels"].get(clean_alias, {})
    expected = previous.get("expected_channel_id") if isinstance(previous, dict) else None
    channel = {
        "schema_version": 1,
        "alias": clean_alias,
        "platform": "youtube",
        "client_secrets_path": str(resolved),
        "client_secrets_path_env": "YOUTUBE_CLIENT_SECRETS_PATH",
        "token_path": str(
            (workspace.data_dir / "credentials" / "youtube" / f"{clean_alias}-token.json").resolve()
        ),
        "token_path_env": "YOUTUBE_TOKEN_PATH",
        "expected_channel_id": expected,
        "enabled": True,
        "configured_at": utc_now(),
    }
    result = {
        "channel": clean_alias,
        "status": "configured",
        "client_secrets_reference": str(resolved),
        "expected_channel_id": expected,
        "scopes": "minimum_read_only",
    }
    if not dry_run:
        config["channels"][clean_alias] = channel
        workspace.write_json("youtube/config.json", config)
        workspace.audit(
            "youtube_configured",
            {
                "channel": clean_alias,
                "client_secrets_path": str(resolved),
                "scopes": "minimum_read_only",
            },
        )
    return result


def pin_expected_channel(workspace: Workspace, alias: str, channel_id: str) -> None:
    config = load_config(workspace)
    channel = config["channels"].get(alias)
    if not isinstance(channel, dict):
        raise ValueError(f"YouTube channel '{alias}' is not configured.")
    expected = channel.get("expected_channel_id")
    if expected and expected != channel_id:
        raise ValueError(
            f"Channel identity mismatch: expected '{expected}', authorized '{channel_id}'."
        )
    if expected is None:
        channel["expected_channel_id"] = channel_id
        workspace.write_json("youtube/config.json", config)
