"""Pipeline B2's crash check (Phase 5b): the model's own short test of the crash, used as a soft
signal, never as a gate.

With each patch the model may send one pytest file: a `test_crash` that makes the call from the
crash report, plus a few tests of neighbouring inputs. The harness places it at the repro path
(never part of a patch) and runs it:

1. **On the buggy code**, once per new check. It must reproduce the crash by the repro gate's
   rule (`repro_gate.judge`: the crash report's exception type, raised from its innermost
   library frame). The tests that fail that way are the check's *reproducing* tests. A check
   that does not reproduce is not used, and the patch is judged as it would be without one: a
   bad check never blocks a patch (the strict gate is what held Pipeline A back).
2. **With the patch**, together with the visible tests. The reproducing tests must now pass,
   and no other test of the check may fail with the reported error. The neighbouring tests are
   held only to that weaker rule, because the model's own expected values could be wrong.

A check is model-written code: it runs in the same sandbox, under the same audit hook, as every
other test run.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from tracerepro.agent.parse import FENCE_RE, PYTHON_TAGS, SEARCH
from tracerepro.agent.repro_gate import REPRO_PATH, VALID, GateResult, Target, judge
from tracerepro.agent.workbench import TrialRun, Workbench, tail
from tracerepro.dataset.preflight import crash_target

CHECK_PATH = REPRO_PATH
NONE = "none"  # the reply carried no usable check
_EDIT_MARK = re.compile(rf"^\s*{re.escape(SEARCH)}\s*$", re.M)


@dataclass(frozen=True)
class Check:
    code: str
    gate: GateResult  # the verdict on the buggy code
    reproducing: tuple[str, ...]  # node ids that fail with the target on the buggy code

    @property
    def usable(self) -> bool:
        return self.gate.verdict == VALID and bool(self.reproducing)


def parse_check(reply: str) -> str | None:
    """The reply's single Python code block that is not an edit block, or None. Soft: a reply
    with no check, or with several candidates, simply has no check."""
    blocks: list[str] = []
    current: list[str] | None = None
    for line in reply.splitlines():
        match = FENCE_RE.match(line.strip())
        if current is None:
            if match and match.group(1).lower() in PYTHON_TAGS:
                current = []
            continue
        if line.strip().startswith("```"):
            blocks.append("\n".join(current))
            current = None
            continue
        current.append(line)
    found = [b for b in blocks if b.strip() and not _EDIT_MARK.search(b)]
    return found[0].rstrip() + "\n" if len(found) == 1 else None


def _wanted(target: Target) -> tuple[str, str, str]:
    return (target.exception, target.file, target.function)


def run_on_bug(code: str, bench: Workbench, target: Target) -> Check:
    """Run the check alone on the buggy code and record which of its tests reproduce."""
    trial = bench.try_patch(
        "", extra_files={CHECK_PATH: code.encode()}, tests=[CHECK_PATH], baseline=False
    )
    gate = judge(trial, target)
    reproducing: tuple[str, ...] = ()
    if gate.verdict == VALID and trial.run is not None:
        reproducing = tuple(
            sorted(
                nodeid
                for nodeid, failure in trial.run.failures().items()
                if nodeid.startswith(CHECK_PATH) and crash_target(failure)[0] == _wanted(target)
            )
        )
    return Check(code, gate, reproducing)


def after_patch(check: Check, trial: TrialRun, target: Target) -> str | None:
    """None when the patch passes the check, else why not (for the feedback)."""
    if trial.run is None or not trial.finished:
        return "Your crash check did not finish with the patch applied."
    outcomes = trial.run.outcomes()
    failures = trial.run.failures()
    still = [n for n in check.reproducing if outcomes.get(n) != "passed"]
    still += sorted(
        n
        for n, failure in failures.items()
        if n.startswith(CHECK_PATH)
        and n not in check.reproducing
        and crash_target(failure)[0] == _wanted(target)
    )
    if not still:
        return None
    details = [t.longrepr for t in trial.run.tests if t.nodeid in still and t.longrepr] or [
        trial.output_tail
    ]
    return (
        "Your crash check still fails with the patch applied: "
        + ", ".join(still)
        + f"\n\n{tail(details[0])}"
    )
