"""Plain-text report formatting."""

from collections.abc import Mapping, Sequence

from tiny_lib import util

Row = Mapping[str, object]


def _widest(rows: Sequence[Row]) -> int:
    return max(len(util.label(r)) for r in rows)


def format_row(row: Row, width: int) -> str:
    return f"{util.label(row):<{width}}  {util.amount(row):>10.2f}"


def format_report(rows: Sequence[Row], title: str = "Report") -> str:
    """Render rows as an aligned two-column report with a total line."""
    width = _widest(rows)
    lines = [title, "-" * max(width + 12, len(title))]
    lines += [format_row(r, width) for r in rows]
    total = sum(util.amount(r) for r in rows)
    lines.append(f"{'TOTAL':<{width}}  {total:>10.2f}")
    return "\n".join(lines)
