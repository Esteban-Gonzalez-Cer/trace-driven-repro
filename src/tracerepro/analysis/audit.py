"""The manual audit's hand-made files (spec §7.5), read by `report` when they exist.

- `benchmark/audit/correct.yaml`: every patch graded correct, one entry per distinct patch, with
  a decision: `ok`, `ok_with_note` or `not_a_fix` (P5.3).
- `benchmark/audit/incorrect_sample.yaml`: a 25% random sample of the patches graded incorrect,
  each judged `real_fix` (the grader rejected a valid fix: a false negative) or `not_a_fix`
  (P5.4). The sample is drawn by `draw_sample` (fixed seed), so anyone can check it:

      uv run python -m tracerepro.analysis.audit sample

The automatic grade stays the headline (docs/ANALYSIS_PLAN.md). Audit findings are reported next
to it: a `not_a_fix` trial is counted as a failure only in the audit-adjusted numbers.
"""

from __future__ import annotations

import math
import random
from collections import Counter
from pathlib import Path
from typing import Any

import yaml

from tracerepro.analysis import stats

CORRECT_DECISIONS = ("ok", "ok_with_note", "not_a_fix")
INCORRECT_DECISIONS = ("real_fix", "not_a_fix")
SAMPLE_FRACTION = 0.25
SAMPLE_SEED = stats.BOOTSTRAP_SEED  # 20261111, the analysis plan's seed
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


def incorrect_population(rows: list[Row]) -> list[str]:
    """Trials graded incorrect that produced a patch (a run stopped before patching has
    nothing for the grader to have wrongly rejected), sorted by bug, pipeline and trial."""
    found = [r for r in rows if not r["solved"] and r.get("patch_sha")]
    return [r["id"] for r in sorted(found, key=lambda r: (r["bug"], r["pipeline"], r["trial"]))]


def draw_sample(
    ids: list[str], fraction: float = SAMPLE_FRACTION, seed: int = SAMPLE_SEED
) -> list[str]:
    """A simple random sample of at least `fraction` of `ids`, reproducible from `seed`."""
    k = math.ceil(fraction * len(ids))
    return sorted(random.Random(seed).sample(ids, k))


def load_incorrect_sample(path: Path, rows: list[Row]) -> dict[str, Any]:
    """The incorrect-sample audit, checked against a fresh draw from the results, with the
    grader's false-negative rate and its 95% Clopper-Pearson interval."""
    data = yaml.safe_load(path.read_text())
    population = incorrect_population(rows)
    expected = draw_sample(population, data["fraction"], data["seed"])
    entries = data["sample"]
    got = sorted(e["trial"] for e in entries)
    if got != expected:
        raise AuditError(f"sample does not match the draw: expected {expected}, got {got}")
    if bad := sorted({e["decision"] for e in entries} - set(INCORRECT_DECISIONS)):
        raise AuditError(f"unknown decisions: {bad}")
    k = sum(e["decision"] == "real_fix" for e in entries)
    lo, hi = stats.clopper_pearson(k, len(entries))
    return {
        "reviewer": data.get("reviewer"),
        "population": len(population),
        "sampled": len(entries),
        "seed": data["seed"],
        "false_negatives": k,
        "false_negative_rate": round(k / len(entries), 4),
        "ci": [round(lo, 4), round(hi, 4)],
        "real_fix": sorted(e["trial"] for e in entries if e["decision"] == "real_fix"),
    }


def incorrect_section(s: dict[str, Any]) -> list[str]:
    lo, hi = s["ci"]
    return [
        "",
        "## Sample of patches graded incorrect (spec §7.5)",
        "",
        f"{s['sampled']} of {s['population']} incorrect patches drawn at random (seed "
        f"{s['seed']}) and reviewed: {s['false_negatives']} were real fixes the grader rejected. "
        f"Estimated false-negative rate {s['false_negative_rate']:.0%} (95% CI {lo:.0%} to "
        f"{hi:.0%}); details in docs/AUDIT.md.",
    ]


def main(argv: list[str] | None = None) -> int:
    import argparse
    import json

    parser = argparse.ArgumentParser(description="Draw the incorrect-patch audit sample.")
    parser.add_argument("command", choices=["sample"])
    parser.add_argument("--results", type=Path, default=Path("benchmark/results/final.jsonl"))
    args = parser.parse_args(argv)
    rows = [json.loads(line) for line in args.results.read_text().splitlines() if line.strip()]
    population = incorrect_population(rows)
    sample = draw_sample(population)
    print(f"{len(sample)} of {len(population)} (seed {SAMPLE_SEED}):")
    for trial in sample:
        print(f"  {trial}")
    return 0


if __name__ == "__main__":
    import sys

    sys.exit(main())
