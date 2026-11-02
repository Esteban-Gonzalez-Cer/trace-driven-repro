"""Automated gold-patch preflight (spec §5.4, plan §5.4, Amendments 1 to 3).

For one candidate (`benchmark/candidates/{id}.yaml`):

1. **Classify the fix.** `c_bug` is the first parent of `c_fix`. Changed files are split into
   tests, docs (dropped under Amendment 3), deny-listed files (exclusion) and source; criterion 3
   is checked on the source files.
2. **Pin and build.** Write the lock, then build `bug_env:{id}` with the image builder.
3. **Find the crash.** Run the fix commit's test files on `c_bug` and with the gold source diff.
   A fix test is an oracle candidate if it fails on `c_bug` with an exception raised outside the
   test (a crash), whose traceback has a library frame, and passes with the gold diff. The target
   is (exception type, innermost library frame). All candidates must agree (Amendment 2 rule 4).
   Other failing fix tests go to `excluded_fix_tests` with the reason (Amendment 1).
4. **Stability.** Run the whole suite on `c_bug` (with the fix test files copied in, as the
   evaluator does) `runs` times. Every oracle candidate must crash the same way each time; tests
   whose outcome varies go to `flaky_set`; tests that pass every time form the baseline.
5. **Gold check and minimal gold files.** Try subsets of the source files, smallest first. The
   first subset under which every oracle test passes and no baseline test fails is `gold_files`.
   If even the full gold diff fails, the preflight fails (checks 1 and 2).
6. **Payload.** Re-run the oracle on `c_bug` with locals captured and build the synthetic payload.

Every run goes through the locked-down runner, so check 4 (offline isolation) holds by
construction. Check 1's "passes on the fix commit" is run as `c_bug` plus the gold source diff,
which equals the fix commit except for test and docs files.

Usage:
    python -m tracerepro.dataset.preflight [CANDIDATE ...]
With no arguments, runs every `benchmark/candidates/*.yaml`.
"""

from __future__ import annotations

import argparse
import datetime as dt
import itertools
import json
import shutil
import sys
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

import docker

from tracerepro.dataset import screening
from tracerepro.dataset.candidate import Candidate, load_candidate, make_lock
from tracerepro.dataset.image_builder import DEFAULT_WORK_DIR, build_image
from tracerepro.dataset.manifest import PROJECT_ROOT, Manifest, TargetFrame, dump_manifest
from tracerepro.dataset.repo import (
    changed_files,
    clone_name,
    diff,
    ensure_clone,
    first_parent,
    show_file,
)
from tracerepro.payload.builder import build_payload, dump_payload
from tracerepro.sandbox.paths import (
    is_config_path,
    is_dependency_or_ci_path,
    is_docs_path,
    is_library_frame,
    is_test_path,
)
from tracerepro.sandbox.runner import (
    FailureInfo,
    Limits,
    PytestRun,
    Workspace,
    copy_into,
    create_workspace,
    remove_workspace,
    run_pytest,
)
from tracerepro.sandbox.validator import ApplyStatus, apply_patch

MAX_SOURCE_FILES = 3
MAX_SOURCE_LINES = 40
PYTEST_INTERNALS = ("/_pytest/", "/pluggy/")
SUITE_ARGS = ("--continue-on-collection-errors",)
PREFLIGHT_LIMITS = Limits(timeout_s=900)


@dataclass(frozen=True)
class Outputs:
    manifests: Path
    payloads: Path
    locks: Path
    screening_log: Path | None

    @classmethod
    def default(cls, root: Path = PROJECT_ROOT) -> Outputs:
        bench = root / "benchmark"
        return cls(
            bench / "manifests", bench / "payloads", bench / "locks", bench / "screening_log.jsonl"
        )


@dataclass
class PreflightResult:
    id: str
    decision: str  # "preflight_pass", "preflight_fail" or "excluded"
    reason: str
    manifest: Manifest | None = None
    stats: dict[str, Any] = field(default_factory=dict)


class _Stop(Exception):
    def __init__(self, decision: str, reason: str) -> None:
        super().__init__(reason)
        self.decision = decision
        self.reason = reason


def _exclude(reason: str) -> _Stop:
    return _Stop("excluded", reason)


def _fail(reason: str) -> _Stop:
    return _Stop("preflight_fail", reason)


Target = tuple[str, str, str]  # (exception type, file, function)


def crash_target(failure: FailureInfo) -> tuple[Target | None, str]:
    """The (type, file, function) target of a failure, or None and the reason it is not a crash."""
    if not failure.frames:
        return None, f"{failure.type} with no traceback"
    innermost = failure.frames[-1][0]
    if is_test_path(innermost) or any(p in innermost for p in PYTEST_INTERNALS):
        return None, f"fails with {failure.type} raised in the test itself, not a crash"
    library = [(file, fn) for file, fn, _ in failure.frames if is_library_frame(file)]
    if not library:
        return None, f"{failure.type} with no library frame in the traceback (Amendment 2)"
    file, function = library[-1]
    return (failure.type, file, function), ""


def subsets_smallest_first(paths: Sequence[str]) -> Iterator[tuple[str, ...]]:
    ordered = sorted(paths)
    for size in range(1, len(ordered) + 1):
        yield from itertools.combinations(ordered, size)


def _rel(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(PROJECT_ROOT))
    except ValueError:
        return str(path.resolve())


class _Preflight:
    def __init__(
        self,
        cand: Candidate,
        *,
        work_dir: Path,
        outputs: Outputs,
        client: docker.DockerClient,
        runs: int,
        limits: Limits,
    ) -> None:
        self.cand = cand
        self.outputs = outputs
        self.client = client
        self.runs = runs
        self.limits = limits
        self.work_dir = work_dir
        self.root = work_dir / "preflight" / cand.id
        self.stats: dict[str, Any] = {}
        self.report: dict[str, Any] = {"id": cand.id, "runs": []}
        self._n = 0

    # --- helpers -----------------------------------------------------------------------------

    @contextmanager
    def workspace(self, patch_files: Sequence[str] = ()) -> Iterator[Workspace]:
        """c_bug from the image, with the fix test files and optionally gold hunks applied."""
        self._n += 1
        ws = create_workspace(self.root / f"ws-{self._n}", self.image, client=self.client)
        try:
            copy_into(ws, self.fix_tests)
            if patch_files:
                result = apply_patch(ws, diff(self.git_dir, self.c_bug, self.cand.c_fix,
                                              list(patch_files)))  # fmt: skip
                if result.status is not ApplyStatus.APPLIED:
                    raise _fail(f"gold diff for {list(patch_files)} {result.status}: "
                                f"{result.message}")  # fmt: skip
            yield ws
        finally:
            remove_workspace(ws)

    def pytest(self, ws: Workspace, label: str, ids: Sequence[str], *extra: str) -> PytestRun:
        run = run_pytest(
            self.image, ids, ws, limits=self.limits, extra_args=[*self.cand.pytest_args, *extra],
            client=self.client,
        )  # fmt: skip
        self.report["runs"].append(
            {
                "label": label,
                "exit_code": run.run.exit_code,
                "exitstatus": run.exitstatus,
                "duration_s": round(run.run.duration_s, 2),
                "timed_out": run.run.timed_out,
                "outcomes": _count(run.outcomes()),
            }
        )
        if run.exitstatus is None:
            tail = run.run.output[-1500:]
            raise _fail(f"pytest did not finish ({label}): timed_out={run.run.timed_out}\n{tail}")
        return run

    def suite(self, ws: Workspace, label: str) -> PytestRun:
        deselect = [arg for t in self.excluded for arg in ("--deselect", t["id"])]
        return self.pytest(ws, label, list(self.cand.test_paths), *SUITE_ARGS, *deselect)

    # --- steps -------------------------------------------------------------------------------

    def classify(self) -> None:
        cand = self.cand
        self.git_dir = ensure_clone(
            cand.repo_location(), self.work_dir / "repos", clone_name(cand.repo), [cand.c_fix]
        )
        self.c_bug = first_parent(self.git_dir, cand.c_fix)
        changed = changed_files(self.git_dir, self.c_bug, cand.c_fix)
        tests = sorted(p for p in changed if is_test_path(p))
        docs = sorted(p for p in changed if p not in tests and is_docs_path(p))
        denied = sorted(
            p for p in changed
            if p not in tests and p not in docs
            and (is_config_path(p) or is_dependency_or_ci_path(p))
        )  # fmt: skip
        source = sorted(set(changed) - set(tests) - set(docs) - set(denied))
        self.source = source
        self.gold_files_full = sorted(set(source) | set(denied))
        lines = sum(changed[p][1] + changed[p][2] for p in source)
        self.stats = {
            "c_bug": self.c_bug,
            "src_files_changed": len(source),
            "src_lines_changed": lines,
            "has_regression_test": any(_is_test_module(p) for p in tests),
        }
        self.report["files"] = {"source": source, "tests": tests, "docs": docs, "denied": denied}
        if denied:
            raise _exclude(f"Criterion 3: the fix changes deny-listed files {denied}.")
        if any(changed[p][1] < 0 for p in source):
            raise _exclude("Criterion 3: the fix changes binary source files.")
        if not any(_is_test_module(p) for p in tests):
            raise _exclude("Criterion 1: the fix commit adds or changes no test module.")
        if not source:
            raise _exclude("Criterion 3: the fix changes no source files.")
        if len(source) > MAX_SOURCE_FILES or lines > MAX_SOURCE_LINES:
            raise _exclude(
                f"Criterion 3: {len(source)} source files and {lines} changed lines "
                f"(limit {MAX_SOURCE_FILES} files, about {MAX_SOURCE_LINES} lines)."
            )
        self.fix_tests = {
            p: show_file(self.git_dir, cand.c_fix, p) for p in tests if changed[p][0] != "D"
        }
        self.fix_modules = sorted(p for p in self.fix_tests if _is_test_module(p))

    def build(self) -> None:
        lock = make_lock(self.cand, self.git_dir, self.c_bug, self.root / "requirements.lock.txt")
        self.lock = lock
        self.draft = Manifest(
            id=self.cand.id,
            repo=self.cand.repo,
            c_bug=self.c_bug,
            c_fix=self.cand.c_fix,
            python_version=self.cand.python_version,
            image_tag=f"bug_env:{self.cand.id}",
            deps_lock=str(lock.resolve()),
        )
        self.image = self.draft.image_tag
        record = build_image(self.draft, work_dir=self.work_dir, client=self.client)
        self.report["image_id"] = record.image_id

    def find_crash(self) -> None:
        with self.workspace() as ws:
            bug = self.pytest(ws, "fix tests on c_bug", self.fix_modules)
        errors = bug.collection_errors()
        if errors:
            nodeid, text = next(iter(sorted(errors.items())))
            first = next((ln for ln in text.splitlines() if ln.startswith("E ")), "")
            raise _exclude(
                f"Criterion 1: the fix tests do not collect on c_bug ({nodeid}: {first.strip()}), "
                "so they cannot reproduce the crash."
            )
        with self.workspace(self.source) as ws:
            fix = self.pytest(ws, "fix tests with gold", self.fix_modules)

        fix_out = fix.outcomes()
        failures = bug.failures()
        targets: dict[str, Target] = {}
        self.excluded: list[dict[str, str]] = []
        for nodeid, outcome in sorted(bug.outcomes().items()):
            if outcome not in ("failed", "error"):
                continue
            failure = failures.get(nodeid)
            if failure is None:
                reason = f"{outcome} on c_bug with no exception recorded"
                target = None
            else:
                target, reason = crash_target(failure)
            with_gold = fix_out.get(nodeid, "missing")
            if target is not None and with_gold != "passed":
                target, reason = None, f"crashes on c_bug but is {with_gold} with the gold diff"
            if target is None:
                self.excluded.append({"id": nodeid, "reason": reason})
            else:
                targets[nodeid] = target

        distinct = sorted(set(targets.values()))
        self.report["targets"] = {k: list(v) for k, v in targets.items()}
        if not distinct:
            reasons = "; ".join(f"{e['id']}: {e['reason']}" for e in self.excluded[:3])
            raise _exclude(
                "Criterion 1: no fix test crashes on c_bug with a library frame"
                + (f" ({reasons})." if reasons else ".")
            )
        if len(distinct) > 1:
            raise _exclude(f"Amendment 2 rule 4: the reproducing fix tests disagree: {distinct}.")
        self.target = distinct[0]
        self.candidates = sorted(targets)
        self.stats["exception"] = self.target[0]

    def stability(self) -> None:
        outcomes: list[dict[str, str]] = []
        for i in range(self.runs):
            with self.workspace() as ws:
                run = self.suite(ws, f"suite on c_bug #{i + 1}")
            failures = run.failures()
            for nodeid in self.candidates:
                failure = failures.get(nodeid)
                target = crash_target(failure)[0] if failure else None
                if target != self.target:
                    raise _fail(
                        f"Check 3 / criterion 4: {nodeid} did not crash the same way in suite "
                        f"run {i + 1} (got {target}, expected {self.target})."
                    )
            outcomes.append(run.outcomes())
        ids = sorted(set().union(*outcomes))
        self.flaky = [t for t in ids if len({o.get(t) for o in outcomes}) > 1]
        stable = [t for t in ids if all(o.get(t) == "passed" for o in outcomes)]
        self.baseline = [t for t in stable if t not in self.candidates]
        if not self.baseline:
            raise _fail("Check 3: no test passes on c_bug, so there is no baseline.")

    def gold(self) -> None:
        last: tuple[list[str], list[str]] = ([], [])
        for subset in subsets_smallest_first(self.source):
            with self.workspace(subset) as ws:
                run = self.suite(ws, f"suite with gold {list(subset)}")
            out = run.outcomes()
            broken_oracle = [t for t in self.candidates if out.get(t) != "passed"]
            regressions = [t for t in self.baseline if out.get(t) != "passed"]
            if not broken_oracle and not regressions:
                self.gold_files = list(subset)
                return
            last = (broken_oracle, regressions)
        broken_oracle, regressions = last
        if broken_oracle:
            raise _fail(
                f"Check 1: oracle tests still fail with the full gold diff: {broken_oracle}"
            )
        raise _fail(f"Check 2: the gold diff breaks baseline tests: {regressions[:10]}")

    def payload(self) -> dict[str, Any]:
        with self.workspace() as ws:
            run = self.pytest(ws, "oracle with locals", self.candidates, "--tracerepro-locals")
            failure = run.failures().get(self.candidates[0])
            if failure is None:
                raise _fail("payload run: the oracle did not fail on c_bug")
            return build_payload(
                self.cand.id, self.c_bug, self.cand.python_version, failure, ws.repo
            )

    def write(self, payload: dict[str, Any]) -> Manifest:
        cand, out = self.cand, self.outputs
        for d in (out.manifests, out.payloads, out.locks):
            d.mkdir(parents=True, exist_ok=True)
        if cand.deps.strategy == "file":
            lock_path = self.lock
        else:
            lock_path = out.locks / f"{cand.id}.txt"
            shutil.copyfile(self.lock, lock_path)
        payload_path = out.payloads / f"{cand.id}.json"
        payload_path.write_text(dump_payload(payload))
        notes = " ".join(filter(None, [cand.notes, f"[split: {cand.split}]"]))
        manifest = replace(
            self.draft,
            issue_url=cand.issue_url,
            fix_date=cand.fix_date,
            target_exception=self.target[0],
            target_frame=TargetFrame(self.target[1], self.target[2]),
            payload_path=_rel(payload_path),
            oracle_tests=tuple(self.candidates),
            excluded_fix_tests=tuple(self.excluded),
            baseline_pass_set=tuple(self.baseline),
            flaky_set=tuple(self.flaky),
            gold_files=tuple(self.gold_files),
            gold_files_full=tuple(self.gold_files_full),
            deps_lock=_rel(lock_path),
            notes=notes,
        )
        source = _rel(cand.source_path) if cand.source_path else f"{cand.id}.yaml"
        header = (
            f"Generated by tracerepro.dataset.preflight from {source}.\n"
            "Do not edit by hand: change the candidate and re-run `make validate-gold`."
        )
        (out.manifests / f"{cand.id}.yaml").write_text(dump_manifest(manifest, header))
        return manifest

    def run(self) -> PreflightResult:
        if self.root.exists():
            shutil.rmtree(self.root)
        self.root.mkdir(parents=True)
        try:
            self.classify()
            self.build()
            self.find_crash()
            self.stability()
            self.gold()
            manifest = self.write(self.payload())
        except _Stop as stop:
            result = PreflightResult(self.cand.id, stop.decision, stop.reason, stats=self.stats)
        else:
            reason = (
                f"All four §5.4 checks pass: {len(self.candidates)} oracle test(s) crash with "
                f"{self.target[0]} from {self.target[1]}::{self.target[2]} and pass with gold "
                f"files {self.gold_files}; {len(self.baseline)} baseline tests stable over "
                f"{self.runs} runs, {len(self.flaky)} flaky; no regressions; offline."
            )
            result = PreflightResult(self.cand.id, "preflight_pass", reason, manifest, self.stats)
        self.report |= {"decision": result.decision, "reason": result.reason}
        (self.root / "report.json").write_text(json.dumps(self.report, indent=2) + "\n")
        return result


def _is_test_module(path: str) -> bool:
    name = Path(path).name
    return name.endswith(".py") and (name.startswith("test_") or name.endswith("_test.py"))


def _count(outcomes: dict[str, str]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for value in outcomes.values():
        counts[value] = counts.get(value, 0) + 1
    return dict(sorted(counts.items()))


def preflight(
    cand: Candidate,
    *,
    work_dir: Path = DEFAULT_WORK_DIR,
    outputs: Outputs | None = None,
    client: docker.DockerClient | None = None,
    runs: int = 3,
    limits: Limits = PREFLIGHT_LIMITS,
) -> PreflightResult:
    outputs = outputs or Outputs.default()
    result = _Preflight(
        cand,
        work_dir=work_dir,
        outputs=outputs,
        client=client or docker.from_env(),
        runs=runs,
        limits=limits,
    ).run()
    if outputs.screening_log is not None:
        screening.upsert(
            outputs.screening_log,
            {
                "id": cand.id,
                "repo": cand.repo,
                "issue_url": cand.issue_url,
                "fix_pr_url": cand.fix_pr_url,
                "c_bug": result.stats.get("c_bug"),
                "c_fix": cand.c_fix,
                "fix_date": cand.fix_date,
                "exception": result.stats.get("exception"),
                "src_files_changed": result.stats.get("src_files_changed"),
                "src_lines_changed": result.stats.get("src_lines_changed"),
                "has_regression_test": result.stats.get("has_regression_test"),
                "decision": result.decision,
                "reason": result.reason,
                "screened_on": dt.date.today().isoformat(),
            },
        )
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument("candidates", nargs="*", type=Path)
    parser.add_argument("--work-dir", type=Path, default=DEFAULT_WORK_DIR)
    parser.add_argument("--runs", type=int, default=3, help="stability runs on c_bug")
    args = parser.parse_args(argv)
    paths = args.candidates or sorted((PROJECT_ROOT / "benchmark" / "candidates").glob("*.yaml"))
    if not paths:
        print("no candidates", file=sys.stderr)
        return 1
    failures = 0
    for path in paths:
        try:
            result = preflight(load_candidate(path), work_dir=args.work_dir, runs=args.runs)
        except Exception as exc:
            failures += 1
            print(f"ERROR {path}: {type(exc).__name__}: {exc}", file=sys.stderr)
            continue
        print(f"{result.decision:15} {result.id}: {result.reason}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
