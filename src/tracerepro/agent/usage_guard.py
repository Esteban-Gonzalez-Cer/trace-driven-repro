"""Limits that keep a benchmark run from draining the Claude plan by accident.

- `TokenBudget`: the per-run token cap shared by both pipelines (spec §6, budget matching).
  When it is used up the pipeline stops and submits its best patch so far.
- `UsageGuard`: pacing across runs, kept in a small ledger file so it survives restarts. It
  waits a minimum interval between real calls and refuses calls past a per-hour or per-day
  count (`PacingLimitReached`), so the runner stops cleanly and resumes later.

Cached answers never touch the guard: they use no plan usage.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from tracerepro.agent.errors import PacingLimitReached

HOUR = 3600.0
DAY = 24 * HOUR


@dataclass
class TokenBudget:
    cap: int
    used: int = 0

    @property
    def exhausted(self) -> bool:
        return self.used >= self.cap

    @property
    def remaining(self) -> int:
        return max(0, self.cap - self.used)

    def charge(self, tokens: int) -> None:
        self.used += tokens


@dataclass(frozen=True)
class GuardLimits:
    max_calls_per_hour: int = 60
    max_calls_per_day: int = 400
    min_interval_s: float = 2.0


DEFAULT_GUARD_LIMITS = GuardLimits()


class UsageGuard:
    def __init__(
        self,
        ledger: Path,
        limits: GuardLimits = DEFAULT_GUARD_LIMITS,
        *,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.ledger = ledger
        self.limits = limits
        self._clock = clock
        self._sleep = sleep

    def _load(self) -> list[float]:
        try:
            data = json.loads(self.ledger.read_text())
        except (OSError, ValueError):
            return []
        calls = data.get("calls") if isinstance(data, dict) else None
        if not isinstance(calls, list):
            return []
        return sorted(float(t) for t in calls if isinstance(t, int | float))

    def _save(self, calls: list[float]) -> None:
        self.ledger.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.ledger.with_suffix(".tmp")
        tmp.write_text(json.dumps({"calls": calls}))
        tmp.replace(self.ledger)

    def before_call(self) -> None:
        now = self._clock()
        calls = [t for t in self._load() if t > now - DAY]
        last_hour = [t for t in calls if t > now - HOUR]
        if len(calls) >= self.limits.max_calls_per_day:
            raise PacingLimitReached(
                f"{len(calls)} calls in the last 24 hours (limit {self.limits.max_calls_per_day})",
                retry_at=calls[0] + DAY,
            )
        if len(last_hour) >= self.limits.max_calls_per_hour:
            raise PacingLimitReached(
                f"{len(last_hour)} calls in the last hour (limit {self.limits.max_calls_per_hour})",
                retry_at=last_hour[0] + HOUR,
            )
        if calls:
            wait = calls[-1] + self.limits.min_interval_s - now
            if wait > 0:
                self._sleep(wait)

    def after_call(self) -> None:
        now = self._clock()
        calls = [t for t in self._load() if t > now - DAY]
        calls.append(now)
        self._save(calls)
