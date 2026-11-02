"""Oracle (held-out) regression test: fails on buggy/, passes on fixed/."""

from tiny_lib import format_report


def test_empty_report_does_not_crash() -> None:
    lines = format_report([], title="Empty").splitlines()
    assert lines[0] == "Empty"
    assert lines[-1].split() == ["TOTAL", "0.00"]
