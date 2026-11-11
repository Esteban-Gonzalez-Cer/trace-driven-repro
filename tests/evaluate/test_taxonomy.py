"""Failure taxonomy (spec §8): one label per failed run, by precedence. Pure data, no Docker."""

from typing import Any

import pytest

from tracerepro.evaluate.taxonomy import PRECEDENCE, first, label_run


def attempt(stage: str, outcome: str, label: str | None) -> dict[str, Any]:
    return {"stage": stage, "outcome": outcome, "label": label}


def result(diff: str | None = "d", status: str = "submitted", *attempts: dict[str, Any]) -> dict:
    return {"final_diff": diff, "status": status, "attempts": list(attempts)}


def verdict(kind: str = "incorrect", **extra: Any) -> dict[str, Any]:
    return {"kind": kind, "regressions": [], "context_coverage": {}, **extra}


MISS = {"fault_localization_miss": True}
SYNTAX = "syntax_or_import_error"


@pytest.mark.parametrize(
    ("pipeline", "res", "ver", "expected"),
    [
        ("B", result(), verdict("correct"), (None, True)),
        ("B", result(), verdict("infra_error"), (None, False)),
        ("B", result(), verdict("tampered"), ("tampered", False)),
        ("B", result(), verdict("scope_violation"), ("scope_violation", False)),
        ("B", result(), verdict("apply_failed"), ("syntax_or_import_error", False)),
        ("B", result(), verdict(), ("incorrect_patch", False)),
        ("B", result(), verdict(regressions=["t"]), ("regression_failure", False)),
        # A missed fault localization outranks a regression and an incorrect patch.
        ("B", result(), verdict(regressions=["t"], context_coverage=MISS),
         ("fault_localization_miss", False)),
        # No patch: the attempts decide. Scope violation outranks a broken reply.
        ("B", result(None, "no_patch", attempt("patch", "contract_failure", SYNTAX),
                     attempt("patch", "scope_violation", "scope_violation")),
         verdict("no_patch"), ("scope_violation", False)),
        ("B", result(None, "no_patch", attempt("patch", "declined", "declined"),
                     attempt("patch", "model_failure", "model_failure")),
         verdict("no_patch", context_coverage=MISS), ("syntax_or_import_error", False)),
        # Pipeline A, strict rule: no valid repro, no patch.
        ("A", result(None, "no_repro", attempt("repro", "no_repro", "no_repro"),
                     attempt("repro", "no_repro", "no_repro")),
         verdict("no_repro"), ("no_repro", False)),
        ("A", result(None, "invalid_repro", attempt("repro", "no_repro", "no_repro"),
                     attempt("repro", "broken", "syntax_or_import_error")),
         verdict("invalid_repro"), ("syntax_or_import_error", False)),
        # Pipeline A with a patch: the repro oracle's verdict outranks localization and the rest.
        ("A", result(), verdict(repro_oracle={"kind": "invalid_repro"}, context_coverage=MISS),
         ("invalid_repro", False)),
        ("A", result(), verdict(repro_oracle={"kind": "valid"}, regressions=["t"]),
         ("regression_failure", False)),
        # A correct patch is solved even if the repro test fails the repro oracle.
        ("A", result(), verdict("correct", repro_oracle={"kind": "no_repro"}), (None, True)),
        # Repro categories never apply to Pipeline B.
        ("B", result(), verdict(repro_oracle={"kind": "no_repro"}), ("incorrect_patch", False)),
    ],
)  # fmt: skip
def test_labels(pipeline: str, res: dict, ver: dict, expected: tuple[str | None, bool]) -> None:
    assert label_run(pipeline, res, ver) == expected


def test_precedence_matches_the_spec() -> None:
    assert PRECEDENCE == (
        "scope_violation", "syntax_or_import_error", "no_repro", "invalid_repro",
        "fault_localization_miss", "state_deficit", "regression_failure", "incorrect_patch",
    )  # fmt: skip
    assert first(["incorrect_patch", "no_repro", "regression_failure"]) == "no_repro"
    assert first(["declined", "unknown"]) is None
