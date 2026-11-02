"""Baseline tests: pass on both the buggy and the fixed version."""

from tiny_lib import format_report
from tiny_lib.util import amount, label


def test_rows_are_aligned_and_totalled() -> None:
    out = format_report([{"name": "apples", "amount": 3}, {"name": "kiwi", "amount": 1.5}])
    lines = out.splitlines()
    assert lines[0] == "Report"
    assert lines[2] == "apples        3.00"
    assert lines[3] == "kiwi          1.50"
    assert lines[-1].split() == ["TOTAL", "4.50"]


def test_custom_title() -> None:
    assert format_report([{"name": "x", "amount": 1}], title="Q4").splitlines()[0] == "Q4"


def test_missing_name_is_question_mark() -> None:
    assert label({"amount": 2}) == "?"


def test_missing_amount_is_zero() -> None:
    assert amount({"name": "x"}) == 0.0
