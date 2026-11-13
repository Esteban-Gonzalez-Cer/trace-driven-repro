"""The manual audit's hand-made files (spec §7.5), read by `report` when they exist.

- `benchmark/audit/correct.yaml`: every patch graded correct, one entry per distinct patch, with
  a decision: `ok`, `ok_with_note` or `not_a_fix` (P5.3).

The automatic grade stays the headline (docs/ANALYSIS_PLAN.md). Audit findings are reported next
to it: a `not_a_fix` trial is counted as a failure only in the audit-adjusted numbers.
"""

from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import Any

import yaml

CORRECT_DECISIONS = ("ok", "ok_with_note", "not_a_fix")
Row = dict[str, Any]


class AuditError(ValueError):
    """An audit file does not match the results it audits."""


def load_correct(path: Path, rows: list[Row]) -> dict[str, Any]:
    """The correct-patch audit, checked against the results: every trial graded correct appears
    exactly once, and nothing else does."""
    data = yaml.safe_load(path.read_text())
    entries = data["patches"]
    seen: Counter[str] = Counter(t for e in entries for t in e["trials"])
    correct = {r["id"] for r in rows if r["solved"]}
    if twice := sorted(t for t, n in seen.items() if n > 1):
        raise AuditError(f"trials listed more than once: {twice}")
    if missing := sorted(correct - set(seen)):
        raise AuditError(f"correct trials not audited: {missing}")
    if extra := sorted(set(seen) - correct):
        raise AuditError(f"audited trials that are not graded correct: {extra}")
    if bad := sorted({e["decision"] for e in entries} - set(CORRECT_DECISIONS)):
        raise AuditError(f"unknown decisions: {bad}")
    decision = {t: e["decision"] for e in entries for t in e["trials"]}
    pipeline = {r["id"]: r["pipeline"] for r in rows}
    out: dict[str, Any] = {
        "reviewer": data.get("reviewer"),
        "patches": len(entries),
        "by_decision": {},
    }
    for d in CORRECT_DECISIONS:
        trials = [t for t, x in decision.items() if x == d]
        out["by_decision"][d] = {
            "patches": sum(e["decision"] == d for e in entries),
            "trials": len(trials),
            "by_pipeline": dict(sorted(Counter(pipeline[t] for t in trials).items())),
        }
    out["not_a_fix"] = sorted(t for t, x in decision.items() if x == "not_a_fix")
    return out


def adjusted_rows(rows: list[Row], not_a_fix: list[str]) -> list[Row]:
    """The rows with every `not_a_fix` trial counted as a failure."""
    drop = set(not_a_fix)
    return [r | {"solved": False} if r["id"] in drop else r for r in rows]


def section(audit: dict[str, Any], adjusted: dict[str, Any] | None) -> list[str]:
    """Markdown lines for the tables file."""
    c = audit["correct"]
    lines = [
        "",
        "## Audit of patches graded correct (spec §7.5)",
        "",
        "Every trial graded correct was reviewed next to the maintainers' fix "
        f"(first pass: {c['reviewer']}): {c['patches']} distinct patches; details in "
        "docs/AUDIT.md.",
        "",
        "| decision | distinct patches | trials | by pipeline |",
        "|---|---|---|---|",
    ]
    for d, x in c["by_decision"].items():
        per = ", ".join(f"{p} {n}" for p, n in x["by_pipeline"].items()) or "none"
        lines.append(f"| {d} | {x['patches']} | {x['trials']} | {per} |")
    if adjusted is None:
        lines += ["", "No patch was judged `not_a_fix`, so the audit changes no number."]
    else:
        counts = "; ".join(f"{p} {a['k']}/{a['n']}" for p, a in adjusted.items())
        lines += ["", f"Audit-adjusted (not_a_fix counted as failures): {counts}."]
    return lines
