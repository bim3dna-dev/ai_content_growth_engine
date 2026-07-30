"""Local artifact storage with atomic writes and idempotency receipts."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


class WorkspaceError(RuntimeError):
    """Raised for invalid or missing workspaces."""


def utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def stable_id(prefix: str, payload: object) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    digest = hashlib.sha256(canonical.encode()).hexdigest()[:16]
    return f"{prefix}_{digest}"


def write_text_atomic(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent, prefix=f".{path.name}.", suffix=".tmp"
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(value)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_path, path)
    finally:
        temporary_path.unlink(missing_ok=True)


def write_json_atomic(path: Path, value: object) -> None:
    serialized = json.dumps(value, indent=2, ensure_ascii=False) + "\n"
    write_text_atomic(path, serialized)


_SENSITIVE_KEY_PARTS = ("secret", "token", "password", "authorization_code", "api_key")


def redact(value: object) -> object:
    """Recursively redact sensitive values before logging or display."""
    if isinstance(value, dict):
        return {
            str(key): (
                "[REDACTED]"
                if any(part in str(key).lower() for part in _SENSITIVE_KEY_PARTS)
                else redact(item)
            )
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact(item) for item in value]
    if isinstance(value, tuple):
        return [redact(item) for item in value]
    return value


@dataclass(frozen=True, slots=True)
class Workspace:
    root: Path

    @property
    def data_dir(self) -> Path:
        return self.root / ".growth-engine"

    @property
    def config_path(self) -> Path:
        return self.data_dir / "config.json"

    def initialize(self, creator: str, niche: str) -> dict[str, Any]:
        config = {
            "schema_version": 1,
            "creator": creator.strip() or "Creator",
            "niche": niche.strip() or "general",
            "platforms": ["youtube", "instagram", "tiktok"],
            "mode": "content_intelligence_read_only",
            "credentials": "environment_only",
            "created_at": utc_now(),
        }
        if self.config_path.exists():
            return self.read_json(self.config_path)
        for relative in (
            "raw/research",
            "raw/metrics",
            "derived/ideas",
            "derived/briefs",
            "reports",
            "jobs",
            "logs",
        ):
            (self.data_dir / relative).mkdir(parents=True, exist_ok=True)
        write_json_atomic(self.config_path, config)
        self.audit(
            "workspace_initialized",
            {"creator": config["creator"], "niche": config["niche"]},
        )
        return config

    def require_initialized(self) -> dict[str, Any]:
        if not self.config_path.is_file():
            raise WorkspaceError(
                f"No Content Growth Engine workspace at '{self.root}'. Run 'growth-engine init'."
            )
        return self.read_json(self.config_path)

    @staticmethod
    def read_json(path: Path) -> dict[str, Any]:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise WorkspaceError(f"Could not read artifact '{path}': {exc}") from exc
        if not isinstance(value, dict):
            raise WorkspaceError(f"Artifact '{path}' must contain a JSON object.")
        return value

    def write_artifact(self, category: str, artifact_id: str, value: object) -> Path:
        path = self.data_dir / category / f"{artifact_id}.json"
        if not path.exists():
            write_json_atomic(path, value)
        return path

    def write_json(self, relative: str, value: object) -> Path:
        path = self.data_dir / relative
        write_json_atomic(path, value)
        return path

    def write_text(self, relative: str, value: str) -> Path:
        path = self.data_dir / relative
        write_text_atomic(path, value)
        return path

    def artifacts(self, category: str) -> list[dict[str, Any]]:
        directory = self.data_dir / category
        if not directory.exists():
            return []
        return [self.read_json(path) for path in sorted(directory.glob("*.json"))]

    def latest(self, category: str) -> dict[str, Any] | None:
        artifacts = self.artifacts(category)
        return max(artifacts, key=lambda item: str(item.get("created_at", "")), default=None)

    def receipt(self, operation: str, inputs: object) -> dict[str, Any] | None:
        job_id = stable_id("job", {"operation": operation, "inputs": inputs})
        path = self.data_dir / "jobs" / f"{job_id}.json"
        return self.read_json(path) if path.is_file() else None

    def write_receipt(self, operation: str, inputs: object, artifact: str) -> None:
        job_id = stable_id("job", {"operation": operation, "inputs": inputs})
        self.write_artifact(
            "jobs",
            job_id,
            {
                "id": job_id,
                "operation": operation,
                "inputs": inputs,
                "artifact": artifact,
                "completed_at": utc_now(),
            },
        )

    def audit(self, event: str, details: dict[str, Any]) -> None:
        path = self.data_dir / "logs" / "audit.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        record = {"at": utc_now(), "event": event, "details": redact(details)}
        with path.open("a", encoding="utf-8", newline="\n") as stream:
            stream.write(json.dumps(record, sort_keys=True, ensure_ascii=False) + "\n")
