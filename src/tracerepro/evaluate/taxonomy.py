"""Failure taxonomy (spec §8): exactly one label for every failed run.

Labels, by precedence (top first). A run gets the first label whose condition holds:

1. `scope_violation`: the submitted patch was rejected by the host validator or scanner, or no
   patch was submitted and a patch attempt was rejected that way.
2. `syntax_or_import_error`: no usable patch or test came out: a reply broke the output format,
   was cut off or refused, gave no fix (`declined`), or the patch did not apply; or Pipeline A's
   repro test does not import.
3. `no_repro` (Pipeline A only): the generated test passes on `c_bug`.
4. `invalid_repro` (Pipeline A only): the test fails for the wrong exception or frame, or fails
   on the fix.
5. `fault_localization_miss`: the lines the fix changes were not shown to the agent
   (Amendment 5, line-level).
6. `state_deficit`: labelled by hand later (Phase 5); never assigned here.
7. `regression_failure`: the patch breaks a stable baseline test.
8. `incorrect_patch`: the patch applies and keeps the baseline, but fails the oracle.

Outside the spec's list (reported separately, never merged into a category):

- `tampered` (Amendment 4): the run's results cannot be trusted; counted as a failure.
- no label for `infra_error`: the trial did not finish and is rerun.

For Pipeline A, categories 3 and 4 come from the repro oracle (spec §7.4) when a test exists;
when the run stopped without a valid repro (the strict rule), they come from the agent-time
gate, whose last stage's attempts are compared by precedence.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

SCOPE_VIOLATION = "scope_violation"
SYNTAX_OR_IMPORT_ERROR = "syntax_or_import_error"
NO_REPRO = "no_repro"
INVALID_REPRO = "invalid_repro"
FAULT_LOCALIZATION_MISS = "fault_localization_miss"
STATE_DEFICIT = "state_deficit"
REGRESSION_FAILURE = "regression_failure"
INCORRECT_PATCH = "incorrect_patch"
TAMPERED = "tampered"
PRECEDENCE = (
    SCOPE_VIOLATION,
    SYNTAX_OR_IMPORT_ERROR,
    NO_REPRO,
    INVALID_REPRO,
    FAULT_LOCALIZATION_MISS,
    STATE_DEFICIT,
    REGRESSION_FAILURE,
    INCORRECT_PATCH,
)
# Attempt labels written by the agent stage, as taxonomy categories.
ATTEMPT_LABELS = {
    "scope_violation": SCOPE_VIOLATION,
    "syntax_or_import_error": SYNTAX_OR_IMPORT_ERROR,
    "declined": SYNTAX_OR_IMPORT_ERROR,
    "model_failure": SYNTAX_OR_IMPORT_ERROR,
    "no_repro": NO_REPRO,
    "invalid_repro": INVALID_REPRO,
    "regression_failure": REGRESSION_FAILURE,
    "incorrect_patch": INCORRECT_PATCH,
}
REPRO_KINDS = {
    "broken": SYNTAX_OR_IMPORT_ERROR,
    "no_repro": NO_REPRO,
    "invalid_repro": INVALID_REPRO,
}


def first(labels: Sequence[str]) -> str | None:
    ranked = [label for label in labels if label in PRECEDENCE]
    return min(ranked, key=PRECEDENCE.index) if ranked else None


def label_run(
    pipeline: str, result: Mapping[str, Any], verdict: Mapping[str, Any]
) -> tuple[str | None, bool]:
    """(label, solved) for one trial. `result` is the agent's record (`AgentResult.to_dict()`),
    `verdict` the evaluator's `verdict.json` data. A solved run has no label."""
    kind = verdict.get("kind")
    if kind == "correct":
        return None, True
    if kind == "infra_error":
        return None, False
    if kind == TAMPERED:
        return TAMPERED, False
    if kind == "scope_violation":
        return SCOPE_VIOLATION, False

    attempts = result.get("attempts", [])
    repro = verdict.get("repro_oracle") or {}
    fl_miss = bool((verdict.get("context_coverage") or {}).get("fault_localization_miss"))

    if result.get("final_diff") is None:
        # No patch: why the run ended. Pipeline A that never got a valid repro: the repro stage.
        stage = "repro" if result.get("status") in (NO_REPRO, INVALID_REPRO) else "patch"
        found = [ATTEMPT_LABELS.get(a.get("label") or "", "") for a in attempts
                 if a.get("stage") == stage]  # fmt: skip
        label = first(found)
        if label is None:
            label = NO_REPRO if result.get("status") == NO_REPRO else SYNTAX_OR_IMPORT_ERROR
        return label, False

    if kind == "apply_failed":
        return SYNTAX_OR_IMPORT_ERROR, False
    candidates: list[str] = []
    if pipeline == "A" and repro.get("kind") in REPRO_KINDS:
        candidates.append(REPRO_KINDS[repro["kind"]])
    if fl_miss:
        candidates.append(FAULT_LOCALIZATION_MISS)
    if verdict.get("regressions"):
        candidates.append(REGRESSION_FAILURE)
    candidates.append(INCORRECT_PATCH)
    return first(candidates), False
