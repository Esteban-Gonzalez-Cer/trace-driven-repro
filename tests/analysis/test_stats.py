"""The analysis plan's statistics against published reference values."""

import pytest

from tracerepro.analysis.stats import (
    bootstrap_paired_difference,
    clopper_pearson,
    cohort,
    collapse,
    mcnemar_exact,
)


@pytest.mark.parametrize(
    ("k", "n", "lower", "upper"),
    [(0, 10, 0.0, 0.3085), (5, 10, 0.1871, 0.8129), (10, 10, 0.6915, 1.0),
     (15, 30, 0.3130, 0.6870), (1, 30, 0.0008, 0.1722)],
)  # fmt: skip
def test_clopper_pearson_matches_reference_values(
    k: int, n: int, lower: float, upper: float
) -> None:
    lo, hi = clopper_pearson(k, n)
    assert lo == pytest.approx(lower, abs=1e-4) and hi == pytest.approx(upper, abs=1e-4)
    with pytest.raises(ValueError):
        clopper_pearson(3, 2)


def test_mcnemar_exact() -> None:
    assert mcnemar_exact(1, 6) == pytest.approx(0.125)  # 2 * (1 + 7) / 2**7
    assert mcnemar_exact(0, 7) == pytest.approx(2 / 128)
    assert mcnemar_exact(3, 3) == 1.0 and mcnemar_exact(0, 0) == 1.0


def test_bootstrap_is_reproducible_and_brackets_the_mean() -> None:
    a, b = [1, 1, 0, 1, 0, 1, 1, 0], [0, 1, 0, 0, 0, 1, 0, 0]
    first = bootstrap_paired_difference(a, b)
    assert first == bootstrap_paired_difference(a, b)  # fixed seed
    mean, lo, hi = first
    assert mean == pytest.approx(3 / 8) and lo <= mean <= hi
    with pytest.raises(ValueError):
        bootstrap_paired_difference([1], [])


def test_two_of_three_rule_and_cohorts() -> None:
    assert collapse([True, True, False]) and not collapse([True, False, False])
    assert cohort("2026-06-30") == "pre-cutoff" and cohort("2026-07-01") == "post-cutoff"
