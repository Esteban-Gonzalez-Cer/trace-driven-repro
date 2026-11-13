"""Every table and figure of the analysis plan, from the exported results only (plan §8.2).

    uv run python -m tracerepro.analysis.report [--results benchmark/results] [--out results]

Inputs: `benchmark/results/<name>.jsonl` (written by `analysis.export`), the manifests (bug
kind, fix date) and nothing else: no run folders, no Docker, no keys, no model. Outputs, all
deterministic (same input, same bytes):

- `results/tables.md`: the primary comparison, secondary measures, failure labels, and the
  descriptive tables by bug kind and contamination cohort (docs/ANALYSIS_PLAN.md);
- `results/per_bug.csv`: each bug's trials and 2-of-3 outcome per pipeline;
- `results/numbers.json`: every number above, for the docs and for tests;
- `results/figures/*.svg`: failure labels per pipeline, the per-bug grid, cost versus accuracy.
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import statistics
import sys
from collections import Counter
from pathlib import Path
from typing import Any

import yaml

from tracerepro.analysis import stats
from tracerepro.evaluate.taxonomy import PRECEDENCE, TAMPERED

LABELS = (*PRECEDENCE, TAMPERED)
SHARED_LABELS = tuple(x for x in LABELS if x not in ("no_repro", "invalid_repro"))
Row = dict[str, Any]


def load_rows(results: Path, name: str = "final") -> list[Row]:
    path = results / f"{name}.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def load_bugs(manifests: Path, bugs: set[str]) -> dict[str, dict[str, Any]]:
    """Bug kind, fix date and cohort for each bug in the results."""
    found: dict[str, dict[str, Any]] = {}
    for bug in sorted(bugs):
        m = yaml.safe_load((manifests / f"{bug}.yaml").read_text())
        found[bug] = {
            "kind": m["crash_kind"],
            "fix_date": str(m["fix_date"]),
            "cohort": stats.cohort(str(m["fix_date"])),
        }
    return found


def _interval(k: int, n: int) -> dict[str, Any]:
    lo, hi = stats.clopper_pearson(k, n) if n else (0.0, 1.0)
    return {
        "k": k,
        "n": n,
        "rate": round(k / n, 4) if n else None,
        "ci": [round(lo, 4), round(hi, 4)],
    }


def _dist(values: list[float]) -> dict[str, Any]:
    if not values:
        return {"mean": None, "median": None, "max": None}
    return {
        "mean": round(float(statistics.mean(values)), 1),
        "median": round(float(statistics.median(values)), 1),
        "max": round(float(max(values)), 1),
    }


def analyze(rows: list[Row], bugs: dict[str, dict[str, Any]]) -> dict[str, Any]:
    pipelines = sorted({r["pipeline"] for r in rows})
    by: dict[tuple[str, str], list[Row]] = {}
    for r in rows:
        by.setdefault((r["pipeline"], r["bug"]), []).append(r)
    for trials in by.values():
        trials.sort(key=lambda r: r["trial"])
    names = sorted(bugs)
    solved = {
        p: {b: stats.collapse([r["solved"] for r in by.get((p, b), [])]) for b in names}
        for p in pipelines
    }
    out: dict[str, Any] = {"bugs": len(names), "trials": len(rows), "pipelines": {}}
    for p in pipelines:
        mine = [r for r in rows if r["pipeline"] == p]
        failed = [r for r in mine if not r["solved"]]
        correct = [r for r in mine if r["solved"]]
        per_bug = [by.get((p, b), []) for b in names]
        entry: dict[str, Any] = {
            "trials": len(mine),
            "trials_solved": len(correct),
            "accuracy": _interval(sum(solved[p].values()), len(names)),
            "pass_at_3": _interval(sum(any(r["solved"] for r in ts) for ts in per_bug), len(names)),
            "consistency": _interval(
                sum(bool(ts) and all(r["solved"] for r in ts) for ts in per_bug), len(names)
            ),
            "labels": {x: sum(r["label"] == x for r in failed) for x in LABELS},
            "unlabelled_failures": sum(not r["label"] for r in failed),
            "tokens": _dist([r["tokens"] for r in mine]),
            "calls": sum(r["calls"] for r in mine),
            "calls_per_run": round(sum(r["calls"] for r in mine) / len(mine), 2) if mine else 0,
            "cost_equiv_usd": round(sum(r["cost_equiv_usd"] for r in mine), 2),
            "cost_equiv_usd_per_run": round(sum(r["cost_equiv_usd"] for r in mine) / len(mine), 4)
            if mine
            else 0,
            "agent_seconds": _dist([float(r["agent_seconds"] or 0) for r in mine]),
            "correct_patch_lines": _dist([r["lines_added"] + r["lines_removed"] for r in correct]),
            "correct_patch_files": _dist([r["patch_files"] for r in correct]),
        }
        if any(r["repro_oracle"] is not None for r in mine):
            valid = [r["repro_oracle"] == "valid" for r in mine]
            entry["repro_runs"] = _interval(sum(valid), len(mine))
            entry["repro_bugs"] = _interval(
                sum(stats.collapse([r["repro_oracle"] == "valid" for r in ts]) for ts in per_bug),
                len(names),
            )
            entry["repro_oracle"] = dict(
                sorted(Counter(str(r["repro_oracle"]) for r in mine).items())
            )
        out["pipelines"][p] = entry
    if pipelines == ["A", "B"]:
        a_only = [b for b in names if solved["A"][b] and not solved["B"][b]]
        b_only = [b for b in names if solved["B"][b] and not solved["A"][b]]
        rate = {
            p: [
                sum(r["solved"] for r in by.get((p, b), [])) / max(1, len(by.get((p, b), [])))
                for b in names
            ]
            for p in pipelines
        }
        mean, lo, hi = stats.bootstrap_paired_difference(rate["A"], rate["B"])
        out["comparison"] = {
            "a_only": a_only,
            "b_only": b_only,
            "both": sum(solved["A"][b] and solved["B"][b] for b in names),
            "neither": sum(not solved["A"][b] and not solved["B"][b] for b in names),
            "mcnemar_p": round(stats.mcnemar_exact(len(a_only), len(b_only)), 4),
            "paired_difference": {"mean": round(mean, 4), "ci": [round(lo, 4), round(hi, 4)]},
        }
    groups: dict[str, Any] = {}
    for key in ("kind", "cohort"):
        groups[key] = {}
        for value in sorted({bugs[b][key] for b in names}):
            members = [b for b in names if bugs[b][key] == value]
            groups[key][value] = {
                p: _interval(sum(solved[p][b] for b in members), len(members)) for p in pipelines
            }
    out["groups"] = groups
    out["per_bug"] = {
        b: bugs[b]
        | {
            p: {
                "trials": [r["solved"] for r in by.get((p, b), [])],
                "labels": [r["label"] for r in by.get((p, b), [])],
                "solved": solved[p][b],
            }
            for p in pipelines
        }
        for b in names
    }
    return out


def _pct(x: dict[str, Any]) -> str:
    lo, hi = x["ci"]
    return f"{x['k']}/{x['n']} ({x['rate']:.0%}; 95% CI {lo:.0%} to {hi:.0%})"


def tables(n: dict[str, Any], run: dict[str, Any]) -> str:
    ps = list(n["pipelines"])
    head = "| | " + " | ".join(f"Pipeline {p}" for p in ps) + " |\n|---|" + "---|" * len(ps)

    def line(title: str, cell: Any) -> str:
        return f"| {title} | " + " | ".join(str(cell(n["pipelines"][p])) for p in ps) + " |"

    out = [
        "# Results tables",
        "",
        "Generated by `make analyze` from `benchmark/results/` (do not edit by hand).",
        f"Run `{run.get('run_id', '?')}`: {n['bugs']} bugs, {n['trials']} trials. "
        "A bug is solved if at least 2 of its 3 trials are correct. All analyses are "
        "exploratory (docs/ANALYSIS_PLAN.md).",
        "",
        "## Primary comparison: patch accuracy",
        "",
        head,
        line("Bugs solved (2 of 3)", lambda p: _pct(p["accuracy"])),
        line("Trials correct", lambda p: f"{p['trials_solved']}/{p['trials']}"),
    ]
    if c := n.get("comparison"):
        d = c["paired_difference"]
        out += [
            "",
            f"- Solved by both: {c['both']}; by neither: {c['neither']}.",
            f"- Solved by A only (b = {len(c['a_only'])}): {', '.join(c['a_only']) or 'none'}.",
            f"- Solved by B only (c = {len(c['b_only'])}): {', '.join(c['b_only']) or 'none'}.",
            f"- Exact McNemar test: p = {c['mcnemar_p']:.3f} (two-sided).",
            f"- Paired difference in per-bug success rate (A minus B): {d['mean']:+.3f}, "
            f"95% bootstrap CI {d['ci'][0]:+.3f} to {d['ci'][1]:+.3f} "
            f"({stats.BOOTSTRAP_REPS:,} resamples, seed {stats.BOOTSTRAP_SEED}).",
        ]
    out += [
        "",
        "## Secondary measures",
        "",
        head,
        line("pass@3 (any trial correct)", lambda p: _pct(p["pass_at_3"])),
        line("Consistency (all 3 correct)", lambda p: _pct(p["consistency"])),
        line(
            "Repro valid, per run", lambda p: _pct(p["repro_runs"]) if "repro_runs" in p else "n/a"
        ),
        line(
            "Repro valid, bugs (2 of 3)",
            lambda p: _pct(p["repro_bugs"]) if "repro_bugs" in p else "n/a",
        ),
        line(
            "Tokens per run: mean / median / max",
            lambda p: "{mean:,.0f} / {median:,.0f} / {max:,.0f}".format(**p["tokens"]),
        ),
        line("Model calls (per run)", lambda p: f"{p['calls']} ({p['calls_per_run']})"),
        line(
            "List-price estimate, total (per run)",
            lambda p: f"${p['cost_equiv_usd']:.2f} (${p['cost_equiv_usd_per_run']:.3f})",
        ),
        line(
            "Agent time per run, s: mean / median / max",
            lambda p: "{mean} / {median} / {max}".format(**p["agent_seconds"]),
        ),
        line(
            "Correct patches, lines changed: mean / median / max",
            lambda p: "{mean} / {median} / {max}".format(**p["correct_patch_lines"]),
        ),
        "",
        "The list price is the CLI's estimate at API prices; the runs used a Claude plan, so no "
        "money was spent per call.",
        "",
        "## Failure labels (one per failed trial, spec §8)",
        "",
        head,
    ]
    for label in LABELS:
        out.append(line(label, lambda p, label=label: p["labels"][label]))
    out.append(line("**failed trials**", lambda p: p["trials"] - p["trials_solved"]))
    out += [
        "",
        "`no_repro` and `invalid_repro` apply to Pipeline A only, so the pipelines are compared "
        f"on the other categories: {', '.join(SHARED_LABELS)}. `state_deficit` is assigned by "
        "hand in the audit.",
    ]
    for key, title in (("kind", "By bug kind"), ("cohort", "By contamination cohort")):
        out += ["", f"## {title} (descriptive, no test)", "", head]
        for value, cells in n["groups"][key].items():
            out.append(f"| {value} | " + " | ".join(_pct(cells[p]) for p in ps) + " |")
    out += [
        "",
        "## Per bug",
        "",
        "| bug | kind | cohort | " + " | ".join(ps) + " |",
        "|---|---|---|" + "---|" * len(ps),
    ]
    for bug, b in n["per_bug"].items():
        cells = []
        for p in ps:
            marks = "".join("✓" if s else "✗" for s in b[p]["trials"])
            cells.append(f"{marks} {'**solved**' if b[p]['solved'] else ''}".strip())
        out.append(f"| {bug} | {b['kind']} | {b['cohort']} | " + " | ".join(cells) + " |")
    return "\n".join(out) + "\n"


def per_bug_csv(n: dict[str, Any]) -> str:
    ps = list(n["pipelines"])
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(
        ["bug", "kind", "fix_date", "cohort"]
        + [f"{p}_{x}" for p in ps for x in ("trials", "solved", "labels")]
    )
    for bug, b in n["per_bug"].items():
        cells: list[Any] = [bug, b["kind"], b["fix_date"], b["cohort"]]
        for p in ps:
            cells += [
                "".join("1" if s else "0" for s in b[p]["trials"]),
                int(b[p]["solved"]),
                ";".join(x for x in b[p]["labels"] if x),
            ]
        w.writerow(cells)
    return buf.getvalue()


def write_all(results: Path, manifests: Path, out: Path, name: str = "final") -> list[Path]:
    rows = load_rows(results, name)
    run_path = results / f"{name}-run.json"
    run = json.loads(run_path.read_text()) if run_path.is_file() else {}
    numbers = analyze(rows, load_bugs(manifests, {r["bug"] for r in rows}))
    numbers["run_id"] = run.get("run_id")
    numbers["set_aside"] = run.get("set_aside", {})
    out.mkdir(parents=True, exist_ok=True)
    written = []
    for fname, text in (
        ("tables.md", tables(numbers, run)),
        ("per_bug.csv", per_bug_csv(numbers)),
        ("numbers.json", json.dumps(numbers, indent=2, sort_keys=True, ensure_ascii=False) + "\n"),
    ):
        (out / fname).write_text(text)
        written.append(out / fname)
    from tracerepro.analysis import figures  # matplotlib is imported only when drawing

    written += figures.draw_all(numbers, out / "figures")
    return written


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build every table and figure from results.")
    parser.add_argument("--results", type=Path, default=Path("benchmark/results"))
    parser.add_argument("--manifests", type=Path, default=Path("benchmark/manifests"))
    parser.add_argument("--out", type=Path, default=Path("results"))
    parser.add_argument("--name", default="final")
    args = parser.parse_args(argv)
    for path in write_all(args.results, args.manifests, args.out, args.name):
        print(f"wrote {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
