"""Export a finished run to small, publishable result files (plan §8.2).

    uv run python -m tracerepro.analysis.export runs/<run_id> [--out DIR] [--name final]

The raw run folder (`runs/`, git-ignored) holds every prompt and reply. The analysis needs none
of that text, only per-trial facts, so this writes:

- `<name>.jsonl`: one row per finished trial (bug, pipeline, trial, grade, failure label, repro
  oracle verdict, usage, patch size and hash, context coverage, review flags);
- `<name>-run.json`: the run's id and config, the trial count, and the attempts that were set
  aside and rerun (`trial_n.aborted-*`), counted by the error that stopped them.

`make analyze` reads only these files, so every table and figure can be rebuilt from a clean
clone with no Docker, no keys and no model. Before writing, every value is scanned for private
data (home paths, e-mail addresses, credentials); any hit stops the export (fail closed).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Any

from tracerepro.analysis.summary import count_calls

ROW_FORMAT = 1

# Private data that must never reach a published file. Upstream test data can contain paths
# like "C:/Users/test", so home paths are matched only as Unix home directories.
_PRIVATE = [
    ("home path", re.compile(r"(?<![A-Za-z]:)/(Users|home)/[A-Za-z0-9._-]+")),
    ("e-mail address", re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")),
    (
        "credential",
        re.compile(r"\b(ghp|gho|ghs|github_pat|sk-ant|sk|xox[abp])[-_][A-Za-z0-9_-]{10,}"),
    ),
]


class PrivateDataError(ValueError):
    """A value about to be exported looks like private data."""


def scan(value: Any, where: str = "") -> None:
    """Raise `PrivateDataError` if any string inside `value` matches a private-data pattern."""
    if isinstance(value, dict):
        for k, v in value.items():
            scan(k, where)
            scan(v, f"{where}.{k}" if where else str(k))
    elif isinstance(value, list | tuple):
        for i, v in enumerate(value):
            scan(v, f"{where}[{i}]")
    elif isinstance(value, str):
        for what, pattern in _PRIVATE:
            if pattern.search(value):
                raise PrivateDataError(f"{what} in {where or 'value'}: refusing to export")


def diff_stats(diff: str) -> dict[str, Any]:
    """Files changed and lines added/removed in a unified diff; its sha256 (12 hex) as an id."""
    if not diff.strip():
        return {"patch_sha": None, "patch_files": 0, "lines_added": 0, "lines_removed": 0}
    files = added = removed = 0
    for line in diff.splitlines():
        if line.startswith("diff --git "):
            files += 1
        elif line.startswith("+") and not line.startswith("+++"):
            added += 1
        elif line.startswith("-") and not line.startswith("---"):
            removed += 1
    sha = hashlib.sha256(diff.encode()).hexdigest()[:12]
    return {"patch_sha": sha, "patch_files": files, "lines_added": added, "lines_removed": removed}


def trial_row(folder: Path) -> dict[str, Any]:
    """One trial's facts, from its `record.json`, `verdict.json` and `calls.jsonl`."""
    rec = json.loads((folder / "record.json").read_text())
    ver = json.loads((folder / "verdict.json").read_text())
    result, meta = rec["result"], rec.get("meta", {})
    bug, pipeline, model = folder.parts[-4], folder.parts[-3], folder.parts[-2]
    calls, cost = count_calls(folder)
    latency = 0.0
    log = folder / "calls.jsonl"
    if log.is_file():
        for line in log.read_text().splitlines():
            if line.strip():
                latency += float(json.loads(line).get("latency_s") or 0.0)
    coverage = ver.get("context_coverage") or {}
    repro = ver.get("repro_oracle") or {}
    row: dict[str, Any] = {
        "format": ROW_FORMAT,
        "id": f"{bug}/{pipeline}/{meta.get('trial', folder.name.removeprefix('trial_'))}",
        "bug": bug,
        "pipeline": pipeline,
        "model": model,
        "trial": int(meta.get("trial", folder.name.removeprefix("trial_"))),
        "solved": bool(ver.get("solved", ver.get("kind") == "correct")),
        "grade": ver.get("kind"),
        "label": ver.get("label"),
        "repro_oracle": repro.get("kind"),
        "status": result.get("status"),
        "stopped_by": result.get("stopped_by"),
        "final_from": result.get("final_from"),
        "attempts": len(result.get("attempts") or []),
        "tokens": int(result.get("tokens_used", 0)),
        "calls": calls,
        "cost_equiv_usd": round(cost, 6),
        "model_seconds": round(latency, 3),
        "agent_seconds": meta.get("agent_seconds"),
        "context_chars": (result.get("context") or {}).get("chars"),
        "fix_shown": coverage.get("fix_shown"),
        "fault_localization_miss": coverage.get("fault_localization_miss"),
        "regressions": len(ver.get("regressions") or []),
        "flags": list(ver.get("flags") or []),
        "prompt_leaks": len(result.get("prompt_leaks") or []),
    }
    diff = (folder / "final.diff").read_text() if (folder / "final.diff").is_file() else ""
    row |= diff_stats(diff or result.get("final_diff") or "")
    return row


def _set_aside_reason(folder: Path) -> str:
    log = folder / "calls.jsonl"
    if log.is_file():
        for line in reversed(log.read_text().splitlines()):
            if line.strip():
                data = json.loads(line)
                if not data.get("ok"):
                    return str(data.get("error") or "unknown")
                break
    return "unknown"


def export(run_dir: Path) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Rows for every finished trial, and the run's metadata with set-aside attempts."""
    rows: list[dict[str, Any]] = []
    aside: Counter[str] = Counter()
    for folder in sorted(run_dir.glob("*/*/*/trial_*")):
        if not folder.is_dir():
            continue
        if ".aborted-" in folder.name:
            aside[_set_aside_reason(folder)] += 1
            continue
        if not ((folder / "record.json").is_file() and (folder / "verdict.json").is_file()):
            raise ValueError(f"unfinished trial (no record or verdict): {folder}")
        rows.append(trial_row(folder))
    rows.sort(key=lambda r: (r["bug"], r["pipeline"], r["model"], r["trial"]))
    run = json.loads((run_dir / "run.json").read_text()) if (run_dir / "run.json").is_file() else {}
    meta = {
        "format": ROW_FORMAT,
        "run_id": run.get("run_id", run_dir.name),
        "config": run.get("config", {}),
        "trials": len(rows),
        "set_aside": dict(sorted(aside.items())),
    }
    scan(rows, "rows")
    scan(meta, "run")
    return rows, meta


def write(rows: list[dict[str, Any]], meta: dict[str, Any], out: Path, name: str) -> list[Path]:
    out.mkdir(parents=True, exist_ok=True)
    jsonl, run = out / f"{name}.jsonl", out / f"{name}-run.json"
    jsonl.write_text("".join(json.dumps(r, sort_keys=True) + "\n" for r in rows))
    run.write_text(json.dumps(meta, indent=2, sort_keys=True) + "\n")
    return [jsonl, run]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Export a run's per-trial results.")
    parser.add_argument("run_dir", type=Path)
    parser.add_argument("--out", type=Path, default=Path("benchmark/results"))
    parser.add_argument("--name", default="final")
    args = parser.parse_args(argv)
    try:
        rows, meta = export(args.run_dir)
    except PrivateDataError as exc:
        print(f"export stopped: {exc}", file=sys.stderr)
        return 1
    for path in write(rows, meta, args.out, args.name):
        print(f"wrote {path}")
    print(f"{meta['trials']} trials; set aside and rerun: {meta['set_aside']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
