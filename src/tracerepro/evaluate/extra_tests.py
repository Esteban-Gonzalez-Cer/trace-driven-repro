"""Beyond-the-crash tests (Phase 5b fix-quality measure, docs/ANALYSIS_PLAN_B2.md).

    uv run python -m tracerepro.evaluate.extra_tests --results benchmark/results/b2.jsonl \\
        --run B=runs/<old B run> --run B2=runs/<B2 run> --out benchmark/results/b2-extra.json

A fix commit often adds tests that check values rather than a crash. Amendment 1 left them out
of the oracle (`excluded_fix_tests` in the manifest). They still say whether a patch honours
the maintainers' wider contract, so after grading they are run on every correct patch:

1. With the **gold patch**, once per bug: only the tests that pass with it are used (a test that
   fails even with the maintainers' fix measures nothing about the model's patch).
2. With each **correct patch**, in a fresh sandbox from `bug_env:{id}` with the fix commit's
   test files copied in, the same harness plugin and audit hook as the grader.

The result is descriptive and never changes a grade. A run that does not finish, or whose
results cannot be read, counts as not passing.
"""

from __future__ import annotations

import argparse
import json
import sys
import uuid
from pathlib import Path
from typing import Any

import docker

from tracerepro.dataset.image_builder import DEFAULT_WORK_DIR
from tracerepro.dataset.manifest import PROJECT_ROOT, Manifest, load_manifest
from tracerepro.evaluate.patch_oracle import (
    EVAL_LIMITS,
    UNTRUSTED_INPUT_ERRORS,
    bug_repo,
    ensure_image,
    fix_test_files,
    gold_diff,
)
from tracerepro.sandbox.runner import (
    Limits,
    copy_into,
    create_workspace,
    remove_workspace,
    run_pytest,
)
from tracerepro.sandbox.validator import ApplyStatus, apply_patch

FORMAT = 1


def run_tests(
    manifest: Manifest,
    diff: str,
    tests: list[str],
    *,
    work_dir: Path = DEFAULT_WORK_DIR,
    client: docker.DockerClient,
    limits: Limits = EVAL_LIMITS,
) -> dict[str, str]:
    """Outcome of each of `tests` with `diff` applied ("not_run" when the run gave no result)."""
    ensure_image(manifest, client, work_dir)
    fix_tests = fix_test_files(manifest, bug_repo(manifest, work_dir))
    ws = create_workspace(
        work_dir / "eval" / f"{manifest.id}-extra-{uuid.uuid4().hex[:12]}", manifest.image_tag,
        client=client,
    )  # fmt: skip
    try:
        copy_into(ws, fix_tests)
        if apply_patch(ws, diff).status is not ApplyStatus.APPLIED:
            return dict.fromkeys(tests, "not_applied")
        outcomes: dict[str, str] = {}
        try:
            run = run_pytest(manifest.image_tag, tests, ws, limits=limits, client=client)
            trusted = run.exitstatus is not None and not run.run.blocked and not run.results_error
            outcomes = run.outcomes() if trusted else {}
        except UNTRUSTED_INPUT_ERRORS:
            outcomes = {}
    finally:
        remove_workspace(ws)
    return {t: outcomes.get(t, "not_run") for t in tests}


def evaluate(
    rows: list[dict[str, Any]],
    runs: dict[str, Path],
    manifests: Path,
    *,
    work_dir: Path = DEFAULT_WORK_DIR,
    client: docker.DockerClient | None = None,
) -> dict[str, Any]:
    client = client or docker.from_env()
    out: dict[str, Any] = {"format": FORMAT, "bugs": {}, "trials": {}}
    for bug in sorted({r["bug"] for r in rows}):
        manifest = load_manifest(manifests / f"{bug}.yaml")
        excluded = [e["id"] for e in manifest.excluded_fix_tests]
        gold: dict[str, str] = {}
        if excluded:
            diff = gold_diff(manifest, bug_repo(manifest, work_dir))
            gold = run_tests(manifest, diff, excluded, work_dir=work_dir, client=client)
        usable = [t for t in excluded if gold.get(t) == "passed"]
        out["bugs"][bug] = {"excluded": excluded, "gold": gold, "tests": usable}
        if not usable:
            continue
        for r in rows:
            if r["bug"] != bug or not r["solved"]:
                continue
            arm_dir = runs[r["pipeline"]] / bug / "B" / r["model"] / f"trial_{r['trial']}"
            diff = (arm_dir / "final.diff").read_text()
            outcomes = run_tests(manifest, diff, usable, work_dir=work_dir, client=client)
            out["trials"][r["id"]] = {
                "outcomes": outcomes,
                "all_passed": all(v == "passed" for v in outcomes.values()),
            }
            print(f"{r['id']}: {sum(v == 'passed' for v in outcomes.values())}/{len(usable)}")
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument("--results", type=Path, required=True)
    parser.add_argument("--run", action="append", required=True, help="ARM=runs/<run_id>")
    parser.add_argument("--manifests", type=Path, default=PROJECT_ROOT / "benchmark" / "manifests")
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    runs = dict(item.split("=", 1) for item in args.run)
    rows = [json.loads(x) for x in args.results.read_text().splitlines() if x.strip()]
    if missing := sorted({r["pipeline"] for r in rows} - set(runs)):
        parser.error(f"no --run for arms {missing}")
    result = evaluate(rows, {k: Path(v) for k, v in runs.items()}, args.manifests)
    args.out.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
