"""Result integrity checks (Amendment 4, layer 2). Run on the host after an evaluation run.

The patch under test runs in the same Python process as pytest and the harness plugin, so the
results file could in principle be rewritten from inside. These checks make a forgery have to
be consistent across records the patch would each have to fake separately:

- **Canary.** A test the harness appends to an oracle test file (one the agent never saw), with
  a random ordinary-looking name, that always raises the bug's target exception. It must come
  back failed with that type. A forgery that marks every failure as passed, or every failure of
  that type, is caught.
- **Exit code.** The container's exit code must equal the `exitstatus` the plugin recorded.
- **JUnit XML.** pytest's own `junit.xml` must agree on pass / not-pass for every test that
  decides the verdict. It is untrusted XML: size-capped, and DTDs and entities are refused.
- **Console summary.** pytest's final `N failed, M passed ...` line must match the counts in the
  results file.
"""

from __future__ import annotations

import re
import secrets
import xml.etree.ElementTree as ET
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from tracerepro.dataset.manifest import Manifest
from tracerepro.sandbox.runner import PytestRun

MAX_JUNIT_BYTES = 20_000_000
# A number directly followed by the word, so "21194 subtests passed" is not read as "passed".
_SUMMARY_RE = re.compile(
    r"\b([0-9]{1,9}) (passed|failed|errors?|skipped|xfailed|xpassed|deselected)\b"
)
_SUMMARY_LINE_RE = re.compile(r"^=*\s*(?:no tests ran|[0-9]+ \w+.*) in [0-9.]+s\b")
_RANK = {"passed": 0, "skipped": 1, "failed": 2, "error": 3}


@dataclass(frozen=True)
class Canary:
    path: str  # repo-relative path of the test file it lives in (the first oracle test file)
    nodeid: str
    exception: str  # the type the plugin must record, e.g. "ValueError"
    source: bytes  # the whole file: the oracle test file plus the canary at the end


# Appended to an oracle test file the agent has never seen (it comes from the fix commit), with
# an ordinary-looking name, so that code under test cannot tell it apart from a real fix test.
_CANARY_TEMPLATE = """


def {name}():
    import builtins
    import importlib

    parts = {exception!r}.split(".")
    error_type = getattr(builtins, parts[-1], None) if len(parts) == 1 else None
    for cut in range(len(parts) - 1, 0, -1):
        try:
            found = importlib.import_module(".".join(parts[:cut]))
        except ImportError:
            continue
        for attr in parts[cut:]:
            found = getattr(found, attr)
        error_type = found
        break
    try:
        error = error_type({message!r})
    except Exception:
        error = error_type.__new__(error_type)
    raise error
"""
_VERBS = ("handles", "keeps", "rejects", "accepts", "returns", "preserves", "formats", "parses")
_NOUNS = ("empty", "nested", "unicode", "default", "missing", "single", "mixed", "large",
          "trailing", "unsorted", "repeated", "optional")  # fmt: skip
_THINGS = ("input", "values", "items", "rows", "keys", "fields", "names", "options", "order")


def _canary_name(taken: str) -> str:
    rng = secrets.SystemRandom()
    while True:
        name = f"test_{rng.choice(_VERBS)}_{rng.choice(_NOUNS)}_{rng.choice(_THINGS)}"
        if name not in taken:
            return name


def make_canary(manifest: Manifest, oracle_source: bytes, name: str | None = None) -> Canary:
    """A canary appended to the first oracle test file, with a random ordinary test name."""
    if not manifest.oracle_tests or not manifest.target_exception:
        raise ValueError(f"{manifest.id}: manifest has no oracle tests or target exception")
    path = manifest.oracle_tests[0].split("::", 1)[0]
    text = oracle_source.decode("utf-8", errors="replace")
    name = name or _canary_name(text)
    snippet = _CANARY_TEMPLATE.format(
        name=name, exception=manifest.target_exception, message="unexpected input"
    )
    return Canary(path, f"{path}::{name}", manifest.target_exception,
                  oracle_source.rstrip(b"\n") + snippet.encode())  # fmt: skip


def junit_address(nodeid: str) -> tuple[str, str]:
    """The (classname, name) pytest's JUnit writer uses for a node id."""
    path, bracket, params = nodeid.partition("[")
    names = path.split("::")
    names[0] = re.sub(r"\.py$", "", names[0].replace("/", "."))
    names[-1] += bracket + params
    return ".".join(names[:-1]), names[-1]


def read_junit(path: Path) -> dict[tuple[str, str], str]:
    """(classname, name) -> worst outcome. Raises ValueError on hostile or malformed XML."""
    if path.is_symlink() or path.stat().st_size > MAX_JUNIT_BYTES:
        raise ValueError("symlinked or oversized junit.xml")
    data = path.read_bytes()
    if b"<!DOCTYPE" in data or b"<!ENTITY" in data:
        raise ValueError("junit.xml declares a DTD or entities")
    try:
        root = ET.fromstring(data)
    except ET.ParseError as exc:
        raise ValueError(f"junit.xml does not parse: {exc}") from exc
    outcomes: dict[tuple[str, str], str] = {}
    for case in root.iter("testcase"):
        tags = {child.tag for child in case}
        outcome = (
            "error" if "error" in tags
            else "failed" if "failure" in tags
            else "skipped" if "skipped" in tags
            else "passed"
        )  # fmt: skip
        key = (case.get("classname", ""), case.get("name", ""))
        if _RANK[outcome] >= _RANK.get(outcomes.get(key, "passed"), 0):
            outcomes[key] = outcome
    return outcomes


def summary_counts(output: str) -> dict[str, int] | None:
    """Counts from the last pytest summary line in the console output, if there is one."""
    for line in reversed(output.splitlines()):
        if _SUMMARY_LINE_RE.match(line.strip()):
            counts: dict[str, int] = {}
            for number, word in _SUMMARY_RE.findall(line):
                counts["error" if word.startswith("error") else word] = int(number)
            return counts
    return None


def summary_mismatch(
    console: dict[str, int], recorded: dict[str, int], ambiguous: int
) -> dict[str, tuple[int, int]]:
    """Counts on which the console summary and the results file disagree.

    pytest versions disagree on a test that passed itself but has failed subtests: 9.2 counts it
    as failed, 9.1 as passed (found on more-itertools-1182). For those `ambiguous` tests only,
    the console may move up to that many from "failed" to "passed"; everything else must match.
    """
    mismatched = {k: (console.get(k, 0), v) for k, v in recorded.items() if console.get(k, 0) != v}
    moved = console.get("passed", 0) - recorded["passed"]
    if (
        mismatched
        and set(mismatched) <= {"passed", "failed"}
        and 0 < moved <= ambiguous
        and console.get("failed", 0) == recorded["failed"] - moved
    ):
        return {}
    return mismatched


def passed_with_failed_subtests(run: PytestRun) -> set[str]:
    """Tests whose own call passed but that have at least one failed subtest."""
    failed_sub = {t.nodeid for t in run.tests if t.subtest and t.outcome == "failed"}
    return {
        t.nodeid for t in run.tests
        if not t.subtest and t.when == "call" and t.outcome == "passed" and t.nodeid in failed_sub
    }  # fmt: skip


def recorded_counts(run: PytestRun) -> dict[str, int]:
    """The same counts as pytest's summary, computed from the results file.

    pytest counts a failed subtest as "failed", reports passed subtests apart ("subtests
    passed", or not at all with -q), and turns a passed test with failed subtests into a failure.
    """
    with_failed_subtests = {
        t.nodeid for t in run.tests if t.subtest and t.outcome == "failed"
    }  # fmt: skip
    counts = {"passed": 0, "failed": 0, "error": 0}
    for test in run.tests:
        if test.outcome == "error":
            counts["error"] += 1
        elif test.when != "call" or test.outcome not in ("passed", "failed"):
            continue
        elif test.subtest:
            counts["failed"] += test.outcome == "failed"
        elif test.outcome == "passed" and test.nodeid in with_failed_subtests:
            counts["failed"] += 1
        else:
            counts[test.outcome] += 1
    return counts


def check_integrity(run: PytestRun, canary: Canary, decisive: Sequence[str]) -> list[str]:
    """Problems found when cross-checking a finished run. Empty means consistent."""
    problems: list[str] = []
    outcomes = run.outcomes()

    canary_outcome = outcomes.get(canary.nodeid)
    canary_failure = run.failures().get(canary.nodeid)
    if canary_outcome is None and canary.path in run.collection_errors():
        pass  # the file holding it did not import (e.g. the patch broke an import): no canary
        # can run, and the oracle tests in that file fail too, so nothing is hidden
    elif canary_outcome != "failed" or canary_failure is None:
        problems.append(f"canary test reported as {canary_outcome or 'missing'}, not failed")
    elif canary_failure.type != canary.exception:
        problems.append(f"canary failed with {canary_failure.type}, not {canary.exception}")

    if run.run.exit_code != run.exitstatus:
        problems.append(
            f"container exit code {run.run.exit_code} != recorded exitstatus {run.exitstatus}"
        )

    if run.junit_path is None:
        problems.append("pytest wrote no junit.xml")
    else:
        try:
            junit = read_junit(run.junit_path)
        except ValueError as exc:
            problems.append(str(exc))
        else:
            disagree = []
            for nodeid in [*decisive, canary.nodeid]:
                ours = outcomes.get(nodeid, "missing") == "passed"
                theirs = junit.get(junit_address(nodeid), "missing") == "passed"
                if ours != theirs:
                    disagree.append(nodeid)
            if disagree:
                problems.append(f"junit.xml disagrees on {len(disagree)} test(s): {disagree[:5]}")

    summary = summary_counts(run.run.output)
    if summary is None:
        problems.append("no pytest summary line in the console output")
    else:
        recorded = recorded_counts(run)
        mismatched = summary_mismatch(summary, recorded, len(passed_with_failed_subtests(run)))
        if mismatched:
            problems.append(f"console summary disagrees (console, recorded): {mismatched}")
    return problems
