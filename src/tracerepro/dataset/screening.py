"""The screening log (`benchmark/screening_log.jsonl`, spec §5.3): one row per candidate."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

FIELDS = (
    "id",
    "repo",
    "issue_url",
    "fix_pr_url",
    "c_bug",
    "c_fix",
    "fix_date",
    "exception",
    "src_files_changed",
    "src_lines_changed",
    "has_regression_test",
    "decision",
    "reason",
    "screened_on",
)
DECISIONS = ("candidate", "excluded", "preflight_pass", "preflight_fail")


def read_log(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def _ordered(row: dict[str, Any]) -> dict[str, Any]:
    known = {k: row.get(k) for k in FIELDS}
    return known | {k: v for k, v in row.items() if k not in FIELDS}


def upsert(path: Path, row: dict[str, Any]) -> None:
    """Insert or update the row with the same `id`. Fields set to None keep their old value."""
    if row.get("decision") not in DECISIONS:
        raise ValueError(f"decision must be one of {DECISIONS}")
    rows = read_log(path)
    for existing in rows:
        if existing["id"] == row["id"]:
            existing.update({k: v for k, v in row.items() if v is not None})
            break
    else:
        rows.append(row)
    path.write_text("".join(json.dumps(_ordered(r)) + "\n" for r in rows))
