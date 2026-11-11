"""The repro oracle (spec §7.4): is Pipeline A's generated test a faithful reproduction?

A generated test is valid only if all three hold:

1. it fails on `c_bug`;
2. the failure is the manifest's target exception raised from the target frame (file and
   function, not the line), by preflight's own rule (`dataset.preflight.crash_target`);
3. it passes, unchanged, on the fix (`c_bug` plus the gold source diff, as preflight defines
   the fix under Amendment 3).

The test is judged in fresh containers; nothing from the agent's runs is reused. It is
model-written code, so it runs in the same sandbox as any patch, with the same audit hook, a
hidden canary test and the three-way result check (`integrity.check_integrity`). A run whose
results cannot be trusted makes the test `tampered`, never valid.

Unlike the agent-time gate (`agent/repro_gate.py`), which only knows the payload, this oracle
uses the manifest's target and the fix: it is the ground truth reported as the repro rate.
"""

from __future__ import annotations

import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import docker

from tracerepro.agent.repro_gate import REPRO_PATH
from tracerepro.dataset.manifest import Manifest
from tracerepro.dataset.preflight import crash_target
from tracerepro.evaluate.integrity import check_integrity, make_canary
from tracerepro.evaluate.patch_oracle import EVAL_LIMITS, UNTRUSTED_INPUT_ERRORS
from tracerepro.sandbox.runner import (
    CaseResult,
    Limits,
    PytestRun,
    copy_into,
    create_workspace,
    remove_workspace,
    run_pytest,
)
from tracerepro.sandbox.validator import ApplyStatus, apply_patch

VALID = "valid"
NO_REPRO = "no_repro"  # passes on c_bug (taxonomy category 3)
INVALID_REPRO = "invalid_repro"  # wrong exception or frame, or fails on the fix (category 4)
BROKEN = "broken"  # does not import or holds no test (category 2)
TAMPERED = "tampered"  # its results cannot be trusted
INFRA_ERROR = "infra_error"  # the fix itself could not be prepared; never the model's fault


@dataclass(frozen=True)
class ReproVerdict:
    kind: str
    detail: str
    on_bug: str = ""  # what happened on c_bug
    on_fix: str = ""  # what happened on the fix

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _mine(tests: tuple[CaseResult, ...]) -> list[CaseResult]:
    return [t for t in tests if t.nodeid == REPRO_PATH or t.nodeid.startswith(REPRO_PATH + "::")]


def _run(
    manifest: Manifest,
    test: str,
    diff: str | None,
    *,
    work_dir: Path,
    client: docker.DockerClient,
    limits: Limits,
) -> tuple[PytestRun | None, list[str], str]:
    """Run the repro test (plus a hidden canary) on `c_bug`, with `diff` applied if given.
    Returns the run, integrity problems, and a reason when it could not run at all."""
    canary = make_canary(manifest, b"")  # a canary-only file at the first oracle file's path
    ws = create_workspace(
        work_dir / "eval" / f"{manifest.id}-repro-{uuid.uuid4().hex[:12]}", manifest.image_tag,
        client=client,
    )  # fmt: skip
    try:
        if diff is not None and apply_patch(ws, diff).status is not ApplyStatus.APPLIED:
            return None, [], "the gold diff does not apply"
        copy_into(ws, {REPRO_PATH: test.encode(), canary.path: canary.source})
        try:
            run = run_pytest(
                manifest.image_tag, [REPRO_PATH, canary.nodeid], ws, limits=limits, client=client
            )
        except UNTRUSTED_INPUT_ERRORS as exc:
            return None, [f"results refused: {type(exc).__name__}"], ""
        problems: list[str] = []
        if run.run.blocked or run.run.flooded or run.run.refused or run.results_error:
            problems.append("the run was blocked, flooded, or its results refused")
        elif any(
            t.when == "collect" and t.outcome in ("error", "failed") for t in _mine(run.tests)
        ):
            # The test file does not import: pytest stops the whole session, so the canary never
            # runs. Nothing to cross-check, and a forged import error could only make the test
            # look worse (broken), never valid.
            pass
        elif run.exitstatus is not None:
            try:
                ids = sorted({t.nodeid for t in _mine(run.tests)})
                problems = check_integrity(run, canary, ids)
            except UNTRUSTED_INPUT_ERRORS as exc:
                problems = [f"results could not be checked: {type(exc).__name__}"]
        return run, problems, ""
    finally:
        remove_workspace(ws)


def judge_repro(
    manifest: Manifest,
    test: str,
    gold: str,
    *,
    work_dir: Path,
    client: docker.DockerClient | None = None,
    limits: Limits = EVAL_LIMITS,
) -> ReproVerdict:
    """Judge `test` (the repro test file's text) against `manifest`; `gold` is the fix's diff."""
    if manifest.target_exception is None or manifest.target_frame is None:
        raise ValueError(f"{manifest.id}: manifest has no target; run preflight first")
    client = client or docker.from_env()
    wanted = (manifest.target_exception, manifest.target_frame.file, manifest.target_frame.function)

    run, problems, why = _run(manifest, test, None, work_dir=work_dir, client=client,
                              limits=limits)  # fmt: skip
    if problems:
        return ReproVerdict(TAMPERED, "; ".join(problems))
    if run is None:
        return ReproVerdict(INFRA_ERROR, why or "the test could not be run on c_bug")
    if run.exitstatus is None:
        return ReproVerdict(INVALID_REPRO, "the test did not finish on c_bug")
    mine = _mine(run.tests)
    if any(t.when == "collect" and t.outcome in ("error", "failed") for t in mine):
        return ReproVerdict(BROKEN, "the test file does not import or collect on c_bug")
    if not mine:
        return ReproVerdict(BROKEN, "the test file holds no test that pytest collects")
    outcomes = run.outcomes()
    failing = sorted({t.nodeid for t in mine if outcomes.get(t.nodeid) in ("failed", "error")})
    if not failing:
        return ReproVerdict(NO_REPRO, "the test passes on c_bug", on_bug="passed")
    failures = run.failures()
    found = [crash_target(failures[n])[0] for n in failing if n in failures]
    if wanted not in found:
        seen = next((f for f in found if f is not None), None)
        what = f"{seen[0]} from {seen[1]}::{seen[2]}" if seen else "no library crash"
        return ReproVerdict(INVALID_REPRO, f"fails on c_bug with {what}, not the target",
                            on_bug=what)  # fmt: skip
    on_bug = f"{wanted[0]} from {wanted[1]}::{wanted[2]}"

    fixed, problems, why = _run(manifest, test, gold, work_dir=work_dir, client=client,
                                limits=limits)  # fmt: skip
    if problems:
        return ReproVerdict(TAMPERED, "; ".join(problems), on_bug=on_bug)
    if fixed is None:
        return ReproVerdict(INFRA_ERROR, why or "the test could not be run on the fix",
                            on_bug=on_bug)  # fmt: skip
    if fixed.exitstatus is None:
        return ReproVerdict(INVALID_REPRO, "the test did not finish on the fix", on_bug=on_bug)
    fixed_mine = _mine(fixed.tests)
    fixed_out = fixed.outcomes()
    not_passed = sorted({t.nodeid for t in fixed_mine if fixed_out.get(t.nodeid) != "passed"})
    if not fixed_mine or not_passed:
        return ReproVerdict(INVALID_REPRO, f"fails on the fix: {not_passed[:3] or 'no test ran'}",
                            on_bug=on_bug, on_fix="failed")  # fmt: skip
    return ReproVerdict(VALID, "fails on c_bug with the target and passes on the fix",
                        on_bug=on_bug, on_fix="passed")  # fmt: skip
