"""The manual audit's hand-made files (spec §7.5), read by `report` when they exist.

- `benchmark/audit/correct.yaml`: every patch graded correct, one entry per distinct patch, with
  a decision: `ok`, `ok_with_note` or `not_a_fix` (P5.3).
- `benchmark/audit/incorrect_sample.yaml`: a 25% random sample of the patches graded incorrect,
  each judged `real_fix` (the grader rejected a valid fix: a false negative) or `not_a_fix`
  (P5.4). The sample is drawn by `draw_sample` (fixed seed), so anyone can check it:

      uv run python -m tracerepro.analysis.audit sample

- `benchmark/audit/state_deficit.yaml`: for every bug, whether fixing it needs state that the
  crash report does not contain (spec §8 category 6, labelled by hand, P5.5). On those bugs a
  failed trial labelled `regression_failure` or `incorrect_patch` becomes `state_deficit`, by
  the spec's precedence (6 comes before 7 and 8; labels 1 to 5 are never replaced).
- `benchmark/audit/second_review.yaml`: the project owner's independent decisions on a random
  sample of the three files above (`draw_second_review`, fixed seed), with agreement and
  Cohen's kappa per part (P5.5):

      uv run python -m tracerepro.analysis.audit second-review

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


REPLACED_BY_STATE_DEFICIT = ("regression_failure", "incorrect_patch")


def load_state_deficit(path: Path, bugs: set[str]) -> dict[str, Any]:
    """The State Deficit labels, one per bug, checked to cover exactly the bugs in the results."""
    data = yaml.safe_load(path.read_text())
    labels = data["bugs"]
    if missing := sorted(bugs - set(labels)):
        raise AuditError(f"bugs without a State Deficit decision: {missing}")
    if extra := sorted(set(labels) - bugs):
        raise AuditError(f"State Deficit decisions for unknown bugs: {extra}")
    if bad := sorted(b for b, x in labels.items() if not isinstance(x.get("deficit"), bool)):
        raise AuditError(f"deficit must be true or false: {bad}")
    return {
        "reviewer": data.get("reviewer"),
        "bugs": sorted(b for b, x in labels.items() if x["deficit"]),
        "judged": len(labels),
    }


def apply_state_deficit(
    rows: list[Row], deficit_bugs: list[str]
) -> tuple[list[Row], dict[str, int]]:
    """Rows with `state_deficit` in place of labels 7 and 8 on deficit bugs, and how many were
    relabelled per pipeline."""
    deficit = set(deficit_bugs)
    out: list[Row] = []
    changed: Counter[str] = Counter()
    for r in rows:
        if r["bug"] in deficit and not r["solved"] and r["label"] in REPLACED_BY_STATE_DEFICIT:
            r = r | {"label": "state_deficit", "auto_label": r["label"]}
            changed[r["pipeline"]] += 1
        out.append(r)
    return out, dict(sorted(changed.items()))


def state_deficit_note(d: dict[str, Any]) -> list[str]:
    per = ", ".join(f"{p} {n}" for p, n in d["relabelled"].items()) or "none"
    return [
        "",
        f"State Deficit (by hand, first pass: {d['reviewer']}): {len(d['bugs'])} of "
        f"{d['judged']} bugs need state the crash report does not contain "
        f"({', '.join(d['bugs']) or 'none'}). Failed trials relabelled from "
        f"`regression_failure` or `incorrect_patch` to `state_deficit`: {per}.",
    ]


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


SECOND_REVIEW_SIZES = {"correct": 10, "incorrect": 5, "state_deficit": 5}


def first_pass(audit: Path) -> dict[str, dict[str, str]]:
    """Every first-pass decision, by part and reference (`bug#patch`, trial id, or bug)."""
    correct = yaml.safe_load((audit / "correct.yaml").read_text())["patches"]
    sample = yaml.safe_load((audit / "incorrect_sample.yaml").read_text())["sample"]
    deficit = yaml.safe_load((audit / "state_deficit.yaml").read_text())["bugs"]
    return {
        "correct": {f"{e['bug']}#{e['patch']}": e["decision"] for e in correct},
        "incorrect": {e["trial"]: e["decision"] for e in sample},
        "state_deficit": {
            b: "deficit" if x["deficit"] else "no_deficit" for b, x in deficit.items()
        },
    }


def draw_second_review(
    decisions: dict[str, dict[str, str]], seed: int = SAMPLE_SEED
) -> dict[str, list[str]]:
    """A simple random sample of each part's references, reproducible from `seed`."""
    return {
        part: sorted(random.Random(seed).sample(sorted(decisions[part]), k))
        for part, k in SECOND_REVIEW_SIZES.items()
    }


def cohen_kappa(pairs: list[tuple[str, str]]) -> float | None:
    """Cohen's kappa for two raters' labels on the same items (None when undefined)."""
    n = len(pairs)
    if not n:
        return None
    observed = sum(a == b for a, b in pairs) / n
    first, second = Counter(a for a, _ in pairs), Counter(b for _, b in pairs)
    expected = sum(first[x] * second[x] for x in set(first) | set(second)) / (n * n)
    if expected == 1:
        return None
    return (observed - expected) / (1 - expected)


def load_second_review(path: Path, decisions: dict[str, dict[str, str]]) -> dict[str, Any]:
    """Agreement between the first pass and the second reviewer, per part and overall."""
    data = yaml.safe_load(path.read_text())
    expected = draw_second_review(decisions, data["seed"])
    out: dict[str, Any] = {"reviewer": data.get("reviewer"), "seed": data["seed"], "parts": {}}
    all_pairs: list[tuple[str, str]] = []
    for part, refs in expected.items():
        answers = data["parts"][part]
        if sorted(answers) != refs:
            raise AuditError(f"second review of {part} does not match the draw: {refs}")
        pairs = [(decisions[part][ref], answers[ref]["decision"]) for ref in refs]
        kappa = cohen_kappa(pairs)
        out["parts"][part] = {
            "n": len(pairs),
            "agree": sum(a == b for a, b in pairs),
            "kappa": None if kappa is None else round(kappa, 3),
            "disagreements": [ref for ref, (a, b) in zip(refs, pairs, strict=True) if a != b],
        }
        all_pairs += pairs
    out["n"] = len(all_pairs)
    out["agree"] = sum(a == b for a, b in all_pairs)
    return out


def second_review_section(s: dict[str, Any]) -> list[str]:
    lines = [
        "",
        f"## Second reviewer ({s['reviewer']}, random sample, seed {s['seed']})",
        "",
        "| part | cases | agree | Cohen's kappa |",
        "|---|---|---|---|",
    ]
    for part, x in s["parts"].items():
        kappa = "n/a" if x["kappa"] is None else f"{x['kappa']:.2f}"
        lines.append(f"| {part} | {x['n']} | {x['agree']} | {kappa} |")
    lines.append(f"| **all** | {s['n']} | {s['agree']} | |")
    return lines


def main(argv: list[str] | None = None) -> int:
    import argparse
    import json

    parser = argparse.ArgumentParser(description="Draw the audit's random samples.")
    parser.add_argument("command", choices=["sample", "second-review"])
    parser.add_argument("--results", type=Path, default=Path("benchmark/results/final.jsonl"))
    parser.add_argument("--audit", type=Path, default=Path("benchmark/audit"))
    args = parser.parse_args(argv)
    if args.command == "second-review":
        for part, refs in draw_second_review(first_pass(args.audit)).items():
            print(f"{part} ({len(refs)}):")
            for ref in refs:
                print(f"  {ref}")
        return 0
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
