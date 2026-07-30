"""Command-line interface for the Content Growth Engine."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from datetime import date
from pathlib import Path
from typing import Any, NoReturn

from growth_engine import __version__
from growth_engine.models import Platform
from growth_engine.services import (
    conduct_research,
    create_brief,
    create_daily_report,
    generate_ideas,
    rank_ideas,
)
from growth_engine.storage import Workspace, WorkspaceError
from growth_engine.youtube.intelligence import analyze_performance, diagnose_content
from growth_engine.youtube.reporting import generate_youtube_report
from growth_engine.youtube.sync import (
    authorize_youtube,
    configure_youtube,
    sync_all,
    sync_analytics,
    sync_channel,
    sync_videos,
    youtube_status,
)


class CliError(RuntimeError):
    """A user-facing CLI error."""


def _add_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--workspace", type=Path, default=Path.cwd(), help="Project directory (default: current)"
    )
    parser.add_argument("--dry-run", action="store_true", help="Preview without writing artifacts")
    parser.add_argument("--json", action="store_true", dest="as_json", help="Print JSON output")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="growth-engine",
        description="Policy-compliant content intelligence for authentic audience value.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    commands = parser.add_subparsers(dest="command", required=True)

    init = commands.add_parser("init", help="Initialize a local content intelligence workspace")
    init.add_argument("--workspace", type=Path, default=Path.cwd())
    init.add_argument("--creator", default="Creator")
    init.add_argument("--niche", default="general")
    init.add_argument("--dry-run", action="store_true")
    init.add_argument("--json", action="store_true", dest="as_json")

    research = commands.add_parser("research", help="Record a topic and raw observations")
    research.add_argument("topic", nargs="?", default="")
    research.add_argument("--channel", help="Channel or subject to research (alias for topic)")
    research.add_argument(
        "--platform",
        action="append",
        choices=[item.value for item in Platform],
        dest="platforms",
    )
    research.add_argument("--observation", action="append", default=[])
    _add_common(research)

    ideas = commands.add_parser("ideas", help="Generate or rank content ideas")
    ideas_commands = ideas.add_subparsers(dest="ideas_command", required=True)
    generate = ideas_commands.add_parser("generate", help="Generate original audience-value ideas")
    generate.add_argument("--research-id")
    generate.add_argument("--count", type=int, default=5)
    _add_common(generate)
    rank = ideas_commands.add_parser("rank", help="Rank ideas with a transparent rubric")
    rank.add_argument("--idea-set-id")
    _add_common(rank)

    brief = commands.add_parser("brief", help="Create content briefs")
    brief_commands = brief.add_subparsers(dest="brief_command", required=True)
    create = brief_commands.add_parser("create", help="Create a draft from a ranked idea")
    create.add_argument("--idea-id")
    _add_common(create)

    report = commands.add_parser("report", help="Create analysis reports")
    report_commands = report.add_subparsers(dest="report_command", required=True)
    daily = report_commands.add_parser("daily", help="Create a local daily pipeline report")
    daily.add_argument("--date", default=date.today().isoformat(), dest="report_date")
    _add_common(daily)

    youtube = commands.add_parser("youtube", help="Read-only official YouTube integration")
    youtube_commands = youtube.add_subparsers(dest="youtube_command", required=True)
    configure = youtube_commands.add_parser(
        "configure", help="Configure an installed-app OAuth client reference"
    )
    configure.add_argument("--client-secrets", type=Path, required=True)
    configure.add_argument("--channel-alias", required=True)
    _add_common(configure)
    auth = youtube_commands.add_parser("auth", help="Authorize read-only YouTube access")
    auth.add_argument("--channel", required=True)
    _add_common(auth)
    status = youtube_commands.add_parser("status", help="Check authorization and channel identity")
    status.add_argument("--channel", required=True)
    _add_common(status)
    sync = youtube_commands.add_parser("sync", help="Synchronize official YouTube data")
    sync_commands = sync.add_subparsers(dest="sync_command", required=True)
    sync_channel_parser = sync_commands.add_parser("channel", help="Synchronize channel metadata")
    sync_channel_parser.add_argument("--channel", required=True)
    _add_common(sync_channel_parser)
    sync_videos_parser = sync_commands.add_parser("videos", help="Synchronize uploaded videos")
    sync_videos_parser.add_argument("--channel", required=True)
    sync_videos_parser.add_argument("--max-items", type=int, default=100)
    _add_common(sync_videos_parser)
    sync_analytics_parser = sync_commands.add_parser(
        "analytics", help="Synchronize owner-authorized analytics"
    )
    sync_analytics_parser.add_argument("--channel", required=True)
    sync_analytics_parser.add_argument("--start-date", required=True)
    sync_analytics_parser.add_argument("--end-date", required=True)
    _add_common(sync_analytics_parser)
    sync_all_parser = sync_commands.add_parser("all", help="Run the complete read-only sync")
    sync_all_parser.add_argument("--channel", required=True)
    sync_all_parser.add_argument("--start-date", required=True)
    sync_all_parser.add_argument("--end-date", required=True)
    sync_all_parser.add_argument("--max-items", type=int, default=100)
    _add_common(sync_all_parser)

    analytics = commands.add_parser(
        "analytics", help="Local deterministic performance intelligence"
    )
    analytics_commands = analytics.add_subparsers(dest="analytics_command", required=True)
    analyze = analytics_commands.add_parser("analyze", help="Calculate local performance metrics")
    analyze.add_argument("--channel", required=True)
    analyze.add_argument("--start-date", required=True)
    analyze.add_argument("--end-date", required=True)
    _add_common(analyze)
    diagnose = analytics_commands.add_parser("diagnose", help="Run rule-based local diagnoses")
    diagnose.add_argument("--channel", required=True)
    diagnose.add_argument("--start-date", required=True)
    diagnose.add_argument("--end-date", required=True)
    _add_common(diagnose)

    youtube_report = report_commands.add_parser(
        "youtube", help="Generate JSON and Markdown YouTube reports"
    )
    youtube_report.add_argument("--channel", required=True)
    youtube_report.add_argument("--start-date", required=True)
    youtube_report.add_argument("--end-date", required=True)
    _add_common(youtube_report)
    return parser


def _print_result(result: dict[str, Any], *, as_json: bool, dry_run: bool) -> None:
    if as_json:
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return
    prefix = "DRY RUN — " if dry_run else ""
    artifact_id = result.get("id", "workspace")
    state = result.get("state", result.get("mode", "initialized"))
    print(f"{prefix}{artifact_id} [{state}]")
    if "topic" in result:
        print(f"Topic: {result['topic']}")
    if isinstance(result.get("ideas"), list):
        for idea in result["ideas"]:
            rank = f"{idea['rank']}. " if idea.get("rank") else "- "
            score = f" (score {idea['score']})" if idea.get("score") is not None else ""
            print(f"{rank}{idea['title']}{score}")
    if "title" in result and "outline" in result:
        print(f"Brief: {result['title']}")
    if "pipeline" in result:
        pipeline = result["pipeline"]
        print(
            "Pipeline: "
            f"{pipeline['research_records']} research, "
            f"{pipeline['idea_sets']} idea sets, "
            f"{pipeline['ranked_ideas']} ranked ideas, "
            f"{pipeline['briefs']} briefs"
        )
        for recommendation in result["recommendations"]:
            print(f"- {recommendation}")


def _parse_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise CliError("--date must be in YYYY-MM-DD format.") from exc


def run(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    workspace = Workspace(args.workspace.resolve())
    if args.command == "init":
        if args.dry_run:
            result: dict[str, Any] = {
                "creator": args.creator,
                "niche": args.niche,
                "mode": "content_intelligence_read_only",
            }
        else:
            result = workspace.initialize(args.creator, args.niche)
    elif args.command == "research":
        if args.topic and args.channel:
            raise CliError("Provide either a topic or --channel, not both.")
        platforms = args.platforms or [item.value for item in Platform]
        result = conduct_research(
            workspace,
            args.channel or args.topic,
            platforms,
            args.observation,
            dry_run=args.dry_run,
        )
    elif args.command == "ideas" and args.ideas_command == "generate":
        if args.count < 1 or args.count > 20:
            raise CliError("--count must be between 1 and 20.")
        result = generate_ideas(
            workspace, args.count, args.research_id, dry_run=args.dry_run
        )
    elif args.command == "ideas" and args.ideas_command == "rank":
        result = rank_ideas(workspace, args.idea_set_id, dry_run=args.dry_run)
    elif args.command == "brief" and args.brief_command == "create":
        result = create_brief(workspace, args.idea_id, dry_run=args.dry_run)
    elif args.command == "report" and args.report_command == "daily":
        result = create_daily_report(
            workspace, _parse_date(args.report_date), dry_run=args.dry_run
        )
    elif args.command == "youtube" and args.youtube_command == "configure":
        result = configure_youtube(
            workspace,
            args.channel_alias,
            args.client_secrets,
            dry_run=args.dry_run,
        )
    elif args.command == "youtube" and args.youtube_command == "auth":
        result = authorize_youtube(workspace, args.channel, dry_run=args.dry_run)
    elif args.command == "youtube" and args.youtube_command == "status":
        result = youtube_status(workspace, args.channel)
    elif (
        args.command == "youtube"
        and args.youtube_command == "sync"
        and args.sync_command == "channel"
    ):
        result = sync_channel(workspace, args.channel, dry_run=args.dry_run)
    elif (
        args.command == "youtube"
        and args.youtube_command == "sync"
        and args.sync_command == "videos"
    ):
        result = sync_videos(
            workspace, args.channel, args.max_items, dry_run=args.dry_run
        )
    elif (
        args.command == "youtube"
        and args.youtube_command == "sync"
        and args.sync_command == "analytics"
    ):
        result = sync_analytics(
            workspace,
            args.channel,
            args.start_date,
            args.end_date,
            dry_run=args.dry_run,
        )
    elif (
        args.command == "youtube"
        and args.youtube_command == "sync"
        and args.sync_command == "all"
    ):
        result = sync_all(
            workspace,
            args.channel,
            args.start_date,
            args.end_date,
            args.max_items,
            dry_run=args.dry_run,
        )
    elif args.command == "analytics" and args.analytics_command == "analyze":
        result = analyze_performance(
            workspace,
            args.channel,
            args.start_date,
            args.end_date,
            dry_run=args.dry_run,
        )
    elif args.command == "analytics" and args.analytics_command == "diagnose":
        result = diagnose_content(
            workspace,
            args.channel,
            args.start_date,
            args.end_date,
            dry_run=args.dry_run,
        )
    elif args.command == "report" and args.report_command == "youtube":
        result = generate_youtube_report(
            workspace,
            args.channel,
            args.start_date,
            args.end_date,
            dry_run=args.dry_run,
        )
    else:
        raise CliError("Unknown command.")
    _print_result(result, as_json=args.as_json, dry_run=args.dry_run)
    return (
        1
        if result.get("state")
        in {"failed", "partially_succeeded", "blocked_by_policy", "not_authorized"}
        else 0
    )


def entrypoint() -> NoReturn:
    try:
        raise SystemExit(run())
    except (CliError, WorkspaceError, ValueError, RuntimeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
