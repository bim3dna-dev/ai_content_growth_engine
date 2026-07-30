"""Central policy boundary for platform-facing behavior."""

from __future__ import annotations

from enum import StrEnum


class PolicyViolation(RuntimeError):
    """Raised when a requested capability is outside the allowed policy."""


class Action(StrEnum):
    LOCAL_RESEARCH = "local_research"
    LOCAL_ANALYSIS = "local_analysis"
    OFFICIAL_API_READ = "official_api_read"
    PUBLISH = "publish"
    ENGAGE = "engage"
    CREATE_AUDIENCE_ACCOUNT = "create_audience_account"
    ARTIFICIAL_TRAFFIC = "artificial_traffic"
    BYPASS_PROTECTION = "bypass_protection"
    MANIPULATE_METRICS = "manipulate_metrics"


MILESTONE_1_ALLOWED = frozenset(
    {Action.LOCAL_RESEARCH, Action.LOCAL_ANALYSIS, Action.OFFICIAL_API_READ}
)

ALWAYS_PROHIBITED = frozenset(
    {
        Action.ENGAGE,
        Action.CREATE_AUDIENCE_ACCOUNT,
        Action.ARTIFICIAL_TRAFFIC,
        Action.BYPASS_PROTECTION,
        Action.MANIPULATE_METRICS,
    }
)


def authorize(action: Action, *, official_api: bool = False) -> None:
    """Authorize an action using a deny-by-default policy."""
    if action in ALWAYS_PROHIBITED:
        raise PolicyViolation(f"Action '{action.value}' is prohibited by policy.")
    if action == Action.PUBLISH:
        raise PolicyViolation("Publishing is outside Milestone 1 and requires explicit approval.")
    if action == Action.OFFICIAL_API_READ and not official_api:
        raise PolicyViolation("Platform access must use an official API.")
    if action not in MILESTONE_1_ALLOWED:
        raise PolicyViolation(f"Action '{action.value}' is not allowed in Milestone 1.")
