"""Run summary from stored logs only (plan §7.3, §8.2): no model, no Docker, no network.

    uv run python -m tracerepro.analysis.summary runs/<run_id> [--json]

Reads every `trial_*/record.json` and `verdict.json` under a run folder and reports, per
pipeline:

- trial results: solved, failure labels, status, repro oracle verdicts (Pipeline A);
- per-bug results collapsed by the analysis plan's rule: a bug is **solved if at least 2 of its
  3 trials succeed** (spec §9), with pass@k (any trial) and consistency (all trials);
- usage: tokens per run (mean, max) from the records, model calls and the CLI's list-price
  estimate from the call logs.

Incomplete trials (no `verdict.json`, or folders set aside as `trial_n.aborted-*`) are listed
and never counted.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

MAJORITY = 2  # of 3 trials (spec §9)


@dataclass
class Trial:
    bug: str
    pipeline: str
    model: str
    n: int
    solved: bool
    label: str | None
    status: str
    tokens: int
    calls: int
    cost_equiv_usd: float
    repro: str | None = None
    kind: str | None = None


@dataclass
class Summary:
    run: str
    trials: list[Trial] = field(default_factory=list)
    incomplete: list[str] = field(default_factory=list)

    def by_pipeline(self) -> dict[str, list[Trial]]:
        found: dict[str, list[Trial]] = defaultdict(list)
        for t in self.trials:
            found[t.pipeline].append(t)
        return dict(sorted(found.items()))

    def per_bug(self, pipeline: str) -> dict[str, list[Trial]]:
        found: dict[str, list[Trial]] = defaultdict(list)
        for t in self.by_pipeline().get(pipeline, []):
            found[t.bug].append(t)
        return dict(sorted(found.items()))

    def table(self) -> dict[str, Any]:
        out: dict[str, Any] = {"run": self.run, "incomplete": self.incomplete, "pipelines": {}}
        for pipeline, trials in self.by_pipeline().items():
            bugs = self.per_bug(pipeline)
            solved_bugs = {
                bug: sum(t.solved for t in ts) >= min(MAJORITY, len(ts)) for bug, ts in bugs.items()
            }
            tokens = [t.tokens for t in trials]
            repro = Counter(t.repro for t in trials if t.repro)
            out["pipelines"][pipeline] = {
                "trials": len(trials),
                "trials_solved": sum(t.solved for t in trials),
                "bugs": len(bugs),
                "bugs_solved_2_of_3": sum(solved_bugs.values()),
                "pass_at_k": sum(any(t.solved for t in ts) for ts in bugs.values()),
                "consistent": sum(all(t.solved for t in ts) for ts in bugs.values()),
                "per_bug": {
                    bug: {
                        "solved": solved_bugs[bug],
                        "trials": [t.solved for t in ts],
                        "labels": [t.label for t in ts],
                    }
                    for bug, ts in bugs.items()
                },
                "labels": dict(Counter(t.label for t in trials if t.label)),
                "repro_oracle": dict(repro),
                "tokens_mean": round(statistics.mean(tokens)) if tokens else 0,
                "tokens_max": max(tokens, default=0),
                "calls": sum(t.calls for t in trials),
                "cost_equiv_usd": round(sum(t.cost_equiv_usd for t in trials), 2),
            }
        return out


def count_calls(folder: Path) -> tuple[int, float]:
    path = folder / "calls.jsonl"
    if not path.is_file():
        return 0, 0.0
    calls, cost = 0, 0.0
    for line in path.read_text().splitlines():
        if not line.strip():
            continue
        data = json.loads(line)
        if data.get("ok") and not data.get("cache_hit"):
            calls += 1
            cost += float(data.get("cost_equiv_usd") or 0.0)
    return calls, cost


def summarize(run_dir: Path) -> Summary:
    summary = Summary(run_dir.name)
    for folder in sorted(run_dir.glob("*/*/*/trial_*")):
        if not folder.is_dir():
            continue
        rel = str(folder.relative_to(run_dir))
        if ".aborted-" in folder.name:
            summary.incomplete.append(rel)
            continue
        record, verdict = folder / "record.json", folder / "verdict.json"
        if not (record.is_file() and verdict.is_file()):
            summary.incomplete.append(rel)
            continue
        rec = json.loads(record.read_text())
        ver = json.loads(verdict.read_text())
        result, meta = rec["result"], rec.get("meta", {})
        bug, pipeline, model = folder.parts[-4], folder.parts[-3], folder.parts[-2]
        calls, cost = count_calls(folder)
        summary.trials.append(Trial(
            bug=bug, pipeline=pipeline, model=model, n=int(meta.get("trial", 0)),
            solved=bool(ver.get("solved", ver.get("kind") == "correct")),
            label=ver.get("label"), status=str(result.get("status", "")),
            tokens=int(result.get("tokens_used", 0)), calls=calls, cost_equiv_usd=cost,
            repro=(ver.get("repro_oracle") or {}).get("kind"), kind=ver.get("kind"),
        ))  # fmt: skip
    return summary


def render(table: dict[str, Any]) -> str:
    lines = [f"run {table['run']}"]
    for pipeline, p in table["pipelines"].items():
        lines += [
            "",
            f"Pipeline {pipeline}: {p['bugs_solved_2_of_3']}/{p['bugs']} bugs solved (2 of 3), "
            f"pass@k {p['pass_at_k']}, consistent {p['consistent']}; "
            f"{p['trials_solved']}/{p['trials']} trials",
            f"  tokens per run: mean {p['tokens_mean']:,}, max {p['tokens_max']:,}; "
            f"{p['calls']} calls; list-price estimate ${p['cost_equiv_usd']}",
            f"  failure labels: {p['labels'] or 'none'}",
        ]
        if p["repro_oracle"]:
            lines.append(f"  repro oracle: {p['repro_oracle']}")
        for bug, b in p["per_bug"].items():
            marks = "".join("+" if s else "-" for s in b["trials"])
            lines.append(f"  {bug:22} {marks:4} {'solved' if b['solved'] else '':7} "
                         f"{[x for x in b['labels'] if x]}")  # fmt: skip
    if table["incomplete"]:
        lines += ["", f"incomplete (not counted): {table['incomplete']}"]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Summarize a benchmark run from its logs.")
    parser.add_argument("run_dir", type=Path)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    table = summarize(args.run_dir).table()
    print(json.dumps(table, indent=2) if args.json else render(table))
    return 0


if __name__ == "__main__":
    sys.exit(main())
