from __future__ import annotations

import json
from pathlib import Path

import pytest

from growth_engine.cli import run


def invoke_json(capsys: pytest.CaptureFixture[str], arguments: list[str]) -> dict[str, object]:
    assert run([*arguments, "--json"]) == 0
    output = capsys.readouterr().out
    value = json.loads(output)
    assert isinstance(value, dict)
    return value


def test_full_cli_workflow_is_idempotent(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    workspace_args = ["--workspace", str(tmp_path)]
    config = invoke_json(
        capsys, ["init", "--creator", "Ada", "--niche", "ethical design", *workspace_args]
    )
    assert config["mode"] == "content_intelligence_read_only"

    research = invoke_json(
        capsys,
        [
            "research",
            "adaptive reuse",
            "--platform",
            "youtube",
            "--observation",
            "Viewers need cost trade-offs.",
            *workspace_args,
        ],
    )
    duplicate = invoke_json(
        capsys,
        [
            "research",
            "adaptive reuse",
            "--platform",
            "youtube",
            "--observation",
            "Viewers need cost trade-offs.",
            *workspace_args,
        ],
    )
    assert research["id"] == duplicate["id"]
    assert len(list((tmp_path / ".growth-engine/raw/research").glob("*.json"))) == 1

    ideas = invoke_json(capsys, ["ideas", "generate", "--count", "4", *workspace_args])
    assert len(ideas["ideas"]) == 4  # type: ignore[arg-type]
    ranking = invoke_json(capsys, ["ideas", "rank", *workspace_args])
    ranked = ranking["ideas"]
    assert isinstance(ranked, list)
    assert [idea["rank"] for idea in ranked] == [1, 2, 3, 4]
    duplicate_ranking = invoke_json(capsys, ["ideas", "rank", *workspace_args])
    assert duplicate_ranking["id"] == ranking["id"]

    brief = invoke_json(capsys, ["brief", "create", *workspace_args])
    assert brief["approval_status"] == "not_required_local_draft"
    report = invoke_json(
        capsys, ["report", "daily", "--date", "2026-07-30", *workspace_args]
    )
    assert report["raw_metrics"] == []
    assert "No raw platform metrics" in " ".join(report["recommendations"])  # type: ignore[arg-type]


def test_dry_run_does_not_write(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert (
        run(
            [
                "init",
                "--workspace",
                str(tmp_path),
                "--creator",
                "Ada",
                "--dry-run",
                "--json",
            ]
        )
        == 0
    )
    capsys.readouterr()
    assert not (tmp_path / ".growth-engine").exists()


def test_research_accepts_channel_option(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    invoke_json(capsys, ["init", "--workspace", str(tmp_path)])
    research = invoke_json(
        capsys,
        ["research", "--channel", "activist-art", "--workspace", str(tmp_path)],
    )
    assert research["topic"] == "activist-art"


def test_requires_initialized_workspace(tmp_path: Path) -> None:
    with pytest.raises(Exception, match="Run 'growth-engine init'"):
        run(["research", "topic", "--workspace", str(tmp_path)])
