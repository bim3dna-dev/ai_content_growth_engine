"""A small platform-specific rate-limit primitive for future official API reads."""

from __future__ import annotations

from dataclasses import dataclass, field
from time import monotonic


@dataclass(slots=True)
class RateLimiter:
    """Deterministic in-process fixed-window limiter."""

    limits_per_minute: dict[str, int]
    _windows: dict[str, tuple[float, int]] = field(default_factory=dict)

    def allow(self, platform: str, *, now: float | None = None) -> bool:
        current = monotonic() if now is None else now
        limit = self.limits_per_minute.get(platform, 0)
        started, used = self._windows.get(platform, (current, 0))
        if current - started >= 60:
            started, used = current, 0
        if limit <= 0 or used >= limit:
            self._windows[platform] = (started, used)
            return False
        self._windows[platform] = (started, used + 1)
        return True
