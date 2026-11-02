"""Row accessors shared by the report module."""

from collections.abc import Mapping


def label(row: Mapping[str, object]) -> str:
    """Display label for a row: its name, or '?' when the name is missing."""
    name = row.get("name")
    return str(name) if name is not None else "?"


def amount(row: Mapping[str, object]) -> float:
    """Numeric amount for a row, defaulting to 0."""
    value = row.get("amount", 0)
    return float(value)  # type: ignore[arg-type]
