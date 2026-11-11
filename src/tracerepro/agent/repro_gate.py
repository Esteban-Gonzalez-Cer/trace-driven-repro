"""The agent-time repro gate (development plan §6.5, spec §6 Pipeline A step 2).

A generated test reproduces the crash only if, run on the buggy code, it fails with the crash
report's exception type raised from the crash report's innermost library frame (file and
function, not the exact line). The rule is preflight's own (`dataset.preflight.crash_target`),
so "reproduces the crash" means exactly what it meant when the bug was validated.

The target comes from the payload the agent was given (the exception type and the innermost
in-app frame), never from the manifest or the oracle: the gate knows nothing the agent does not.

Results of a gate run:

- `valid`: a repro test failed with the target exception from the target frame;
- `no_repro`: the test ran and passed on the buggy code (taxonomy category 3);
- `invalid_repro`: it failed, but with another exception or from another place, or the run did
  not finish (category 4);
- `broken`: the test file did not import or contained no test (Syntax or Import Error).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from tracerepro.agent.workbench import TrialRun, tail
from tracerepro.dataset.preflight import crash_target
from tracerepro.sandbox.runner import CaseResult

REPRO_PATH = "repro/test_repro.py"  # where the harness places the test; never part of a patch
VALID = "valid"
NO_REPRO = "no_repro"
INVALID_REPRO = "invalid_repro"
BROKEN = "broken"
LABELS = {
    NO_REPRO: "no_repro",
    INVALID_REPRO: "invalid_repro",
    BROKEN: "syntax_or_import_error",
}


@dataclass(frozen=True)
class Target:
    exception: str  # as the harness plugin names it: `ValueError`, `pkg.errors.Error`
    file: str
    function: str

    def describe(self) -> str:
        return f"{self.exception} raised in {self.file}, function {self.function}"


def payload_target(payload: Mapping[str, Any]) -> Target:
    """The crash's exception type and innermost in-app frame, from the payload."""
    value = payload["exception"]["values"][-1]
    module, name = value.get("module", ""), value["type"]
    exception = name if module in ("", "builtins", "__main__") else f"{module}.{name}"
    frames = [f for f in value["stacktrace"]["frames"] if f.get("in_app")]
    if not frames:
        raise ValueError("the payload has no in-app frame")
    innermost = frames[-1]
    return Target(exception, innermost["filename"], innermost["function"])


@dataclass(frozen=True)
class GateResult:
    verdict: str
    detail: str  # one line: what happened
    report: str  # for the model: the detail plus the end of the failure or run output

    @property
    def label(self) -> str | None:
        return LABELS.get(self.verdict)


def _mine(tests: tuple[CaseResult, ...]) -> list[CaseResult]:
    return [t for t in tests if t.nodeid == REPRO_PATH or t.nodeid.startswith(REPRO_PATH + "::")]


def judge(trial: TrialRun, target: Target) -> GateResult:
    def result(verdict: str, detail: str, more: str) -> GateResult:
        return GateResult(verdict, detail, f"{detail}\n\n{tail(more)}".rstrip())

    if trial.run is None or not trial.finished:
        detail = trial.message or "the test run produced no results"
        return result(INVALID_REPRO, f"The test did not finish: {detail}.", trial.output_tail)
    mine = _mine(trial.run.tests)
    collect = [t for t in mine if t.when == "collect" and t.outcome in ("error", "failed")]
    if collect:
        detail = "The test file could not be imported or collected."
        return result(BROKEN, detail, collect[0].longrepr or trial.output_tail)
    if not mine:
        detail = "The test file contains no test that pytest collects."
        return result(BROKEN, detail, trial.output_tail)
    failures = {k: v for k, v in trial.run.failures().items() if k.startswith(REPRO_PATH)}
    outcomes = trial.run.outcomes()
    wanted = (target.exception, target.file, target.function)
    others = []
    for nodeid, failure in failures.items():
        found, why = crash_target(failure)
        if found == wanted:
            longrepr = next((t.longrepr for t in mine if t.nodeid == nodeid and t.longrepr), "")
            detail = f"The test fails on the current code with {target.describe()}, as required."
            return result(VALID, detail, longrepr or failure.message)
        if found is not None:
            others.append(f"{found[0]} raised in {found[1]}, function {found[2]}")
        else:
            others.append(why)
    failed = [
        k for k, v in outcomes.items() if k.startswith(REPRO_PATH) and v in ("failed", "error")
    ]
    if failed:
        what = others[0] if others else "an assertion or error in the test itself"
        detail = (
            f"The test fails on the current code, but with {what}, not with {target.describe()}."
        )
        return result(INVALID_REPRO, detail, trial.output_tail)
    return result(
        NO_REPRO,
        "The test passes on the current code, so it does not reproduce the crash.",
        trial.output_tail,
    )


def repro_passes(trial: TrialRun) -> str | None:
    """After a patch: None if every repro test passed, else why not (for the feedback)."""
    if trial.run is None or not trial.finished:
        return "The reproduction test did not finish with the patch applied."
    mine = _mine(trial.run.tests)
    outcomes = trial.run.outcomes()
    bad = sorted({t.nodeid for t in mine if outcomes.get(t.nodeid) != "passed"})
    if not mine or bad:
        return "The reproduction test still fails with the patch applied: " + ", ".join(
            bad or [REPRO_PATH]
        )
    return None
