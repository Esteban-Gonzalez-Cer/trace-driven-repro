"""The statistics of the analysis plan (docs/ANALYSIS_PLAN.md, spec §9), written and tested
before any result exists. Pure Python: no scipy, so `make analyze` needs nothing extra.

- `clopper_pearson(k, n)`: exact 95% interval for a proportion.
- `mcnemar_exact(b, c)`: exact two-sided McNemar test on paired per-bug outcomes; `b` bugs are
  solved by A only, `c` by B only.
- `bootstrap_paired_difference(a, b)`: percentile bootstrap interval for the mean paired
  difference (bugs resampled with replacement, fixed seed).
- `collapse(trials)`: the per-bug rule, solved if at least 2 of its 3 trials succeed.
- `cohort(fix_date)`: pre- or post-cutoff for the model (contamination cohorts, descriptive).
"""

from __future__ import annotations

import datetime as dt
import math
import random
from collections.abc import Callable, Sequence

ALPHA = 0.05
BOOTSTRAP_REPS = 10_000
BOOTSTRAP_SEED = 20261111
# Anthropic's stated training data cutoff for claude-sonnet-5-5 (models overview page, read
# 2026-11-11): June 2026. A fix dated on or before the last day is pre-cutoff.
CUTOFF = {"claude-sonnet-5-5": dt.date(2026, 6, 30)}


def _binom_cdf(k: int, n: int, p: float) -> float:
    """P(X <= k) for X ~ Binomial(n, p)."""
    if k < 0:
        return 0.0
    if k >= n:
        return 1.0
    return sum(math.comb(n, i) * p**i * (1 - p) ** (n - i) for i in range(k + 1))


def _solve(f: Callable[[float], float], target: float) -> float:
    """p in [0, 1] with f(p) == target, for f increasing in p (bisection to 1e-30)."""
    lo, hi = 0.0, 1.0
    for _ in range(100):
        mid = (lo + hi) / 2
        if f(mid) < target:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


def clopper_pearson(k: int, n: int, alpha: float = ALPHA) -> tuple[float, float]:
    """Exact (Clopper-Pearson) two-sided interval for k successes in n trials."""
    if n <= 0 or not 0 <= k <= n:
        raise ValueError(f"need 0 <= k <= n and n > 0, got k={k}, n={n}")
    # lower: P(X >= k | p) = alpha/2; upper: P(X <= k | p) = alpha/2 (both monotone in p).
    lower = 0.0 if k == 0 else _solve(lambda p: 1 - _binom_cdf(k - 1, n, p), alpha / 2)
    upper = 1.0 if k == n else _solve(lambda p: 1 - _binom_cdf(k, n, p), 1 - alpha / 2)
    return lower, upper


def mcnemar_exact(b: int, c: int) -> float:
    """Exact two-sided McNemar p-value: under no difference, the discordant pairs split as a
    fair coin, so p = 2 * P(X <= min(b, c)) for X ~ Binomial(b + c, 1/2), capped at 1."""
    if b < 0 or c < 0:
        raise ValueError("counts must be non-negative")
    n = b + c
    if n == 0:
        return 1.0
    return min(1.0, 2 * _binom_cdf(min(b, c), n, 0.5))


def bootstrap_paired_difference(
    a: Sequence[float],
    b: Sequence[float],
    *,
    reps: int = BOOTSTRAP_REPS,
    seed: int = BOOTSTRAP_SEED,
    alpha: float = ALPHA,
) -> tuple[float, float, float]:
    """(mean difference a - b, lower, upper): percentile bootstrap over paired items."""
    if len(a) != len(b) or not a:
        raise ValueError("need two non-empty sequences of the same length")
    diffs = [x - y for x, y in zip(a, b, strict=True)]
    n = len(diffs)
    rng = random.Random(seed)
    means = sorted(sum(rng.choice(diffs) for _ in range(n)) / n for _ in range(reps))
    lo = means[math.floor(alpha / 2 * reps)]
    hi = means[min(reps - 1, math.ceil((1 - alpha / 2) * reps) - 1)]
    return sum(diffs) / n, lo, hi


def collapse(trials: Sequence[bool], majority: int = 2) -> bool:
    """A bug is solved if at least `majority` of its trials succeed (2 of 3, spec §9)."""
    return sum(trials) >= majority


def cohort(fix_date: str | dt.date, model: str = "claude-sonnet-5-5") -> str:
    """'pre-cutoff' or 'post-cutoff' for the model's stated training data cutoff."""
    date = fix_date if isinstance(fix_date, dt.date) else dt.date.fromisoformat(str(fix_date))
    return "pre-cutoff" if date <= CUTOFF[model] else "post-cutoff"
