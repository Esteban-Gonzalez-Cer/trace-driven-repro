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

from tracerepro.analysis import audit as audit_files
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


def analyze(
    rows: list[Row], bugs: dict[str, dict[str, Any]], pair: tuple[str, str] | None = None
) -> dict[str, Any]:
    """Every number of the analysis plan. With two pipelines, `pair` orders the comparison
    (first minus second); by default they are in name order."""
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
        if any("check" in r for r in mine):
            entry["b2"] = b2_measures(mine)
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
    if len(pipelines) == 2:
        first, second = pair or pipelines
        only = {
            p: [b for b in names if solved[p][b] and not solved[q][b]]
            for p, q in ((first, second), (second, first))
        }
        rate = {
            p: [
                sum(r["solved"] for r in by.get((p, b), [])) / max(1, len(by.get((p, b), [])))
                for b in names
            ]
            for p in pipelines
        }
        mean, lo, hi = stats.bootstrap_paired_difference(rate[first], rate[second])
        # Keys are named after the pipelines: `a_only` and `b_only` for the M4 study (A, B).
        out["comparison"] = {
            f"{first.lower()}_only": only[first],
            f"{second.lower()}_only": only[second],
            "both": sum(solved[first][b] and solved[second][b] for b in names),
            "neither": sum(not solved[first][b] and not solved[second][b] for b in names),
            "mcnemar_p": round(stats.mcnemar_exact(len(only[first]), len(only[second])), 4),
            "paired_difference": {"mean": round(mean, 4), "ci": [round(lo, 4), round(hi, 4)]},
        }
        if pair is not None:  # name order (the M4 study) is the default and is not recorded
            out["comparison"]["pair"] = list(pair)
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


def b2_measures(rows: list[Row]) -> dict[str, Any]:
    """Pipeline B2's own measures (docs/ANALYSIS_PLAN_B2.md): the crash check, file requests and
    escalation, as counts of trials."""
    n = len(rows)
    usable = [r for r in rows if r.get("check_usable")]
    return {
        "check_sent": _interval(sum(r.get("check") not in (None, "none") for r in rows), n),
        "check_usable": _interval(len(usable), n),
        "check_verdicts": dict(sorted(Counter(str(r.get("check")) for r in rows).items())),
        "check_caught": _interval(sum(bool(r.get("check_caught")) for r in rows), n),
        "check_caught_then_solved": sum(bool(r.get("check_caught")) and r["solved"] for r in rows),
        "final_passes_check": _interval(
            sum(r.get("check_final") == "passes" for r in usable), len(usable)
        ),
        "passes_check_but_incorrect": sum(
            r.get("check_final") == "passes" and not r["solved"] for r in usable
        ),
        "file_requests": _interval(sum(bool(r.get("files_requested")) for r in rows), n),
        "files_requested": sum(int(r.get("files_requested") or 0) for r in rows),
        "files_granted": sum(int(r.get("files_granted") or 0) for r in rows),
        "escalated": _interval(sum(bool(r.get("escalated")) for r in rows), n),
        "escalated_then_solved": sum(bool(r.get("escalated")) and r["solved"] for r in rows),
    }


def beyond_the_crash(rows: list[Row], extra: dict[str, Any]) -> dict[str, Any]:
    """The maintainers' other new tests (left out of the oracle: Amendment 1), run on every
    correct patch of the bugs that have such tests passing with the gold patch. Read from
    `<name>-extra.json` (`evaluate.extra_tests`); descriptive only."""
    bugs = sorted(b for b, x in extra["bugs"].items() if x["tests"])
    out: dict[str, Any] = {"bugs": bugs, "pipelines": {}}
    for p in sorted({r["pipeline"] for r in rows}):
        correct = [r for r in rows if r["pipeline"] == p and r["solved"] and r["bug"] in bugs]
        missing = [r["id"] for r in correct if r["id"] not in extra["trials"]]
        if missing:
            raise ValueError(f"beyond-the-crash results missing for {missing[:5]}")
        passed = [r["id"] for r in correct if extra["trials"][r["id"]]["all_passed"]]
        out["pipelines"][p] = {
            "correct_trials": _interval(len(passed), len(correct)),
            "failed": sorted(r["id"] for r in correct if r["id"] not in passed),
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
        first, second = c.get("pair", ps)
        one, two = c[f"{first.lower()}_only"], c[f"{second.lower()}_only"]
        out += [
            "",
            f"- Solved by both: {c['both']}; by neither: {c['neither']}.",
            f"- Solved by {first} only (b = {len(one)}): {', '.join(one) or 'none'}.",
            f"- Solved by {second} only (c = {len(two)}): {', '.join(two) or 'none'}.",
            f"- Exact McNemar test: p = {c['mcnemar_p']:.3f} (two-sided).",
            f"- Paired difference in per-bug success rate ({first} minus {second}): "
            f"{d['mean']:+.3f}, "
            f"95% bootstrap CI {d['ci'][0]:+.3f} to {d['ci'][1]:+.3f} "
            f"({stats.BOOTSTRAP_REPS:,} resamples, seed {stats.BOOTSTRAP_SEED}).",
        ]
    if w := n.get("without"):
        acc = "; ".join(f"{p} {_pct(a)}" for p, a in w["accuracy"].items())
        out += ["", f"Without {', '.join(w['bugs'])} (descriptive): bugs solved {acc}."]
        if wc := w.get("comparison"):
            d = wc["paired_difference"]
            out[-1] += (
                f" McNemar p = {wc['mcnemar_p']:.3f}; paired difference {d['mean']:+.3f} "
                f"(95% CI {d['ci'][0]:+.3f} to {d['ci'][1]:+.3f})."
            )
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
    ]
    out += b2_section(n)
    out += [
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
    if "state_deficit" in n.get("audit", {}):
        out += audit_files.state_deficit_note(n["audit"]["state_deficit"])
    if "correct" in n.get("audit", {}):
        out += audit_files.section(n["audit"], n["audit"].get("adjusted"))
    if "incorrect_sample" in n.get("audit", {}):
        out += audit_files.incorrect_section(n["audit"]["incorrect_sample"])
    if "second_review" in n.get("audit", {}):
        out += audit_files.second_review_section(n["audit"]["second_review"])
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


def b2_section(n: dict[str, Any]) -> list[str]:
    """Markdown for Pipeline B2's own measures and the beyond-the-crash tests, when present."""
    lines: list[str] = []
    for p, e in n["pipelines"].items():
        if "b2" not in e:
            continue
        b = e["b2"]
        lines += [
            "",
            f"## Pipeline {p}'s own measures (per trial)",
            "",
            f"- Crash check sent: {_pct(b['check_sent'])}; usable (reproduced the crash on the "
            f"buggy code): {_pct(b['check_usable'])}. Verdicts on the buggy code: "
            + ", ".join(f"{k} {v}" for k, v in b["check_verdicts"].items())
            + ".",
            f"- A patch sent back because its check still failed: {_pct(b['check_caught'])}; "
            f"of those trials, solved in the end: {b['check_caught_then_solved']}.",
            f"- Submitted patch passes its usable check: {_pct(b['final_passes_check'])}; passes "
            f"its check but graded incorrect: {b['passes_check_but_incorrect']}.",
            f"- Trials with a file request: {_pct(b['file_requests'])}; files requested "
            f"{b['files_requested']}, granted {b['files_granted']}.",
            f"- Retries escalated: {_pct(b['escalated'])}; of those trials, solved: "
            f"{b['escalated_then_solved']}.",
        ]
    if beyond := n.get("beyond_the_crash"):
        lines += [
            "",
            "## Beyond-the-crash tests (descriptive)",
            "",
            "The maintainers' other new tests from the fix commit, left out of the oracle because "
            "they check values rather than a crash (Amendment 1), run on every correct patch of "
            f"the {len(beyond['bugs'])} bugs that have such tests passing with the gold patch "
            f"({', '.join(beyond['bugs']) or 'none'}).",
            "",
            "| | " + " | ".join(f"Pipeline {p}" for p in beyond["pipelines"]) + " |",
            "|---|" + "---|" * len(beyond["pipelines"]),
            "| Correct trials that also pass them | "
            + " | ".join(_pct(x["correct_trials"]) for x in beyond["pipelines"].values())
            + " |",
        ]
    return lines


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


def write_all(
    results: Path,
    manifests: Path,
    out: Path,
    name: str = "final",
    audit: Path | None = None,
    pair: tuple[str, str] | None = None,
    without: tuple[str, ...] = (),
) -> list[Path]:
    rows = load_rows(results, name)
    run_path = results / f"{name}-run.json"
    run = json.loads(run_path.read_text()) if run_path.is_file() else {}
    bugs = load_bugs(manifests, {r["bug"] for r in rows})
    found: dict[str, Any] = {}
    if audit is not None and (audit / "state_deficit.yaml").is_file():
        deficit = audit_files.load_state_deficit(audit / "state_deficit.yaml", set(bugs))
        rows, relabelled = audit_files.apply_state_deficit(rows, deficit["bugs"])
        found["state_deficit"] = deficit | {"relabelled": relabelled}
    numbers = analyze(rows, bugs, pair)
    if without:
        kept = {b: x for b, x in bugs.items() if b not in without}
        sub = analyze([r for r in rows if r["bug"] in kept], kept, pair)
        numbers["without"] = {
            "bugs": sorted(set(without) & set(bugs)),
            "accuracy": {p: e["accuracy"] for p, e in sub["pipelines"].items()},
            "comparison": sub.get("comparison"),
        }
    extra_path = results / f"{name}-extra.json"
    if extra_path.is_file():
        numbers["beyond_the_crash"] = beyond_the_crash(rows, json.loads(extra_path.read_text()))
    if audit is not None and (audit / "correct.yaml").is_file():
        correct = audit_files.load_correct(audit / "correct.yaml", rows)
        found["correct"] = correct
        if correct["not_a_fix"]:
            adj = analyze(audit_files.adjusted_rows(rows, correct["not_a_fix"]), bugs, pair)
            found["adjusted"] = {p: e["accuracy"] for p, e in adj["pipelines"].items()}
    if audit is not None and (audit / "incorrect_sample.yaml").is_file():
        sample = audit_files.load_incorrect_sample(audit / "incorrect_sample.yaml", rows)
        found["incorrect_sample"] = sample
    if audit is not None and (audit / "second_review.yaml").is_file():
        decisions = audit_files.first_pass(audit)
        found["second_review"] = audit_files.load_second_review(
            audit / "second_review.yaml", decisions
        )
    if found:
        numbers["audit"] = found
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
    parser.add_argument("--audit", type=Path, default=Path("benchmark/audit"))
    parser.add_argument("--pair", nargs=2, help="paired comparison order: first minus second")
    parser.add_argument("--also-without", nargs="+", default=[],
                        help="bugs also left out in a descriptive second view")  # fmt: skip
    args = parser.parse_args(argv)
    pair = (args.pair[0], args.pair[1]) if args.pair else None
    without = tuple(args.also_without)
    for path in write_all(
        args.results, args.manifests, args.out, args.name, args.audit, pair, without
    ):
        print(f"wrote {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
