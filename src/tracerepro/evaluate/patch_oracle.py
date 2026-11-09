"""Patch oracle (spec §7.3, §7.4, Amendment 4): score one patch for one bug in a fresh container.

1. Validate the diff on the host (spec §7.1). A violation is a Scope Violation.
2. Create a fresh workspace from `bug_env:{id}` (the `c_bug` snapshot). Nothing from any earlier
   run, including the agent's own container, is reused.
3. Copy the fix commit's test files in from `c_fix` (the held-out oracle).
4. Re-apply the patch from the host. A valid patch that does not apply is recorded as such.
5. Scan what the patch added (`sandbox.scanner`). Code that reaches for the test harness is a
   Scope Violation; other suspicious code is flagged for the reviewer.
6. Append a canary test to an oracle test file, then run `oracle_tests` plus
   `baseline_pass_set` plus the canary. The plugin's audit hook blocks access to the results
   directory and logs suspicious actions.
7. Check the run's integrity (`evaluate.integrity`): a blocked action, an unreadable results
   file or an inconsistency between the results, JUnit, the console and the canary is Tampered.
8. Correct only if every oracle test passes and no baseline test fails, **and** the oracle also
   passes in a plain run (fresh sandbox, plain pytest, no harness plugin, no canary). Code that
   fixes the bug only when it detects the grader is Tampered.
9. Compare the run's audit events with the gold patch's; new ones are flagged for the reviewer.
10. A run killed at the time or memory limit is `incorrect` when the gold patch's run finished
    well within the limit (the patch caused it); otherwise it stays `infra_error`.

Flags never change a verdict: they direct the manual audit of correct patches (spec §7.5);
`review_report` renders them. This is the core of the Phase 3 evaluator.
"""

from __future__ import annotations

import enum
import json
import re
import uuid
from dataclasses import dataclass, field, replace
from pathlib import Path

import docker
import docker.errors

from tracerepro.dataset.image_builder import DEFAULT_WORK_DIR, build_image
from tracerepro.dataset.manifest import Manifest
from tracerepro.dataset.repo import changed_files, clone_name, diff, ensure_clone, show_file
from tracerepro.evaluate.integrity import (
    check_integrity,
    junit_address,
    make_canary,
    read_junit,
    summary_counts,
)
from tracerepro.sandbox.paths import is_test_path
from tracerepro.sandbox.runner import (
    JUNIT_FILE,
    OUT_MOUNT,
    AuditEvent,
    Limits,
    PytestRun,
    SandboxError,
    copy_into,
    create_workspace,
    remove_workspace,
    run_pytest,
    run_sandboxed,
)
from tracerepro.sandbox.scanner import HARNESS, Finding, ScanResult, scan_patch
from tracerepro.sandbox.validator import ApplyStatus, Validation, apply_patch, validate

EVAL_LIMITS = Limits(timeout_s=900)
# Errors from reading what came out of a container. That data is untrusted: failing to read it
# means the patch's run is not trusted (`tampered`), never a crash of the grader (red-team
# round 2 crashed it four ways).
UNTRUSTED_INPUT_ERRORS = (
    SandboxError, ValueError, OSError, RecursionError, UnicodeError, KeyError, IndexError,
)  # fmt: skip
GOLD_AUDIT_FILE = "gold-audit.json"
# A run killed at a limit is blamed on the patch when gold finished within this share of it.
GOLD_HEADROOM = 3


class VerdictKind(enum.StrEnum):
    CORRECT = "correct"
    INCORRECT = "incorrect"
    SCOPE_VIOLATION = "scope_violation"
    APPLY_FAILED = "apply_failed"
    TAMPERED = "tampered"  # the run's results cannot be trusted (Amendment 4); a model failure
    INFRA_ERROR = "infra_error"  # the run did not finish; never a model failure


@dataclass(frozen=True)
class Verdict:
    kind: VerdictKind
    message: str = ""
    validation: Validation | None = None
    failing_oracle: tuple[str, ...] = ()
    regressions: tuple[str, ...] = ()
    run: PytestRun | None = field(default=None, compare=False)
    flags: tuple[Finding, ...] = ()  # for the reviewer; never change the verdict
    audit: tuple[AuditEvent, ...] = ()


def fix_test_files(manifest: Manifest, git_dir: Path) -> dict[str, bytes]:
    """Every test file the fix commit added or changed, as it is at `c_fix`."""
    changed = changed_files(git_dir, manifest.c_bug, manifest.c_fix)
    return {
        path: show_file(git_dir, manifest.c_fix, path)
        for path, (status, _, _) in sorted(changed.items())
        if is_test_path(path) and status != "D"
    }


def bug_repo(manifest: Manifest, work_dir: Path = DEFAULT_WORK_DIR) -> Path:
    """The cached bare clone holding both `c_bug` and `c_fix`."""
    return ensure_clone(
        manifest.repo_location(),
        work_dir / "repos",
        clone_name(manifest.repo),
        [manifest.c_bug, manifest.c_fix],
    )


def gold_diff(manifest: Manifest, git_dir: Path) -> str:
    """The human fix restricted to source files (`gold_files_full`; Amendments 1 and 3)."""
    return diff(git_dir, manifest.c_bug, manifest.c_fix, list(manifest.gold_files_full))


def ensure_image(manifest: Manifest, client: docker.DockerClient, work_dir: Path) -> None:
    try:
        client.images.get(manifest.image_tag)
    except docker.errors.ImageNotFound:
        build_image(manifest, work_dir=work_dir, client=client)


def score_patch(
    manifest: Manifest,
    diff: str,
    *,
    work_dir: Path = DEFAULT_WORK_DIR,
    client: docker.DockerClient | None = None,
    limits: Limits = EVAL_LIMITS,
) -> Verdict:
    client = client or docker.from_env()
    verdict = _evaluate(manifest, diff, work_dir=work_dir, client=client, limits=limits)
    if verdict.run is None:
        return verdict
    run = verdict.run.run
    profile = gold_profile(manifest, work_dir=work_dir, client=client, limits=limits)
    if profile is None:
        note = Finding("behaviour_unavailable", "", 0, "the gold patch did not score correct")
        return replace(verdict, flags=(*verdict.flags, note))
    verdict = replace(
        verdict, flags=(*verdict.flags, *behaviour_flags(verdict.audit, profile.behaviour))
    )
    # Red-team finding (failure_laundering): a patch that hangs or eats memory must not turn
    # its failure into an infrastructure error, which never counts against the model.
    killed = run.timed_out or run.oom_killed
    if (
        verdict.kind is VerdictKind.INFRA_ERROR
        and killed
        and profile.duration_s * GOLD_HEADROOM < limits.timeout_s
    ):
        what = "ran out of memory" if run.oom_killed else f"timed out at {limits.timeout_s:.0f}s"
        return replace(
            verdict,
            kind=VerdictKind.INCORRECT,
            message=f"the run {what}; the gold patch's run took {profile.duration_s:.1f}s",
        )
    return verdict


def _evaluate(
    manifest: Manifest,
    diff: str,
    *,
    work_dir: Path,
    client: docker.DockerClient,
    limits: Limits,
) -> Verdict:
    """Steps 1 to 8: everything except the comparison with the gold patch's behaviour."""
    if not manifest.oracle_tests:
        raise ValueError(f"{manifest.id}: manifest has no oracle_tests; run preflight first")
    validation = validate(diff)
    if not validation.ok:
        summary = "; ".join(
            f"{v.rule}: {v.path} {v.message}".strip() for v in validation.violations
        )
        return Verdict(VerdictKind.SCOPE_VIOLATION, summary, validation)

    ensure_image(manifest, client, work_dir)
    git_dir = bug_repo(manifest, work_dir)
    fix_tests = fix_test_files(manifest, git_dir)
    oracle_file = manifest.oracle_tests[0].split("::", 1)[0]
    canary = make_canary(manifest, fix_tests.get(oracle_file, b""))
    decisive = [*manifest.oracle_tests, *manifest.baseline_pass_set]
    ws = create_workspace(
        work_dir / "eval" / f"{manifest.id}-{uuid.uuid4().hex[:12]}", manifest.image_tag,
        client=client,
    )  # fmt: skip
    try:
        copy_into(ws, fix_tests)
        applied = apply_patch(ws, diff)
        if applied.status is ApplyStatus.FAILED:
            return Verdict(VerdictKind.APPLY_FAILED, applied.message, validation)
        try:
            scan = scan_patch(ws.repo, validation.files)
        except Exception as exc:  # fail closed: code the scanner cannot read does not run
            reason = f"the scanner could not analyse the patch ({type(exc).__name__})"
            scan = ScanResult(blocks=(Finding(HARNESS, "", 0, reason),))
        if not scan.ok:
            message = "; ".join(str(b) for b in scan.blocks)
            return Verdict(VerdictKind.SCOPE_VIOLATION, message, validation, flags=scan.flags)
        copy_into(ws, {canary.path: canary.source})
        try:
            run = run_pytest(
                manifest.image_tag, [*decisive, canary.nodeid], ws, limits=limits, client=client
            )
        except UNTRUSTED_INPUT_ERRORS as exc:
            return Verdict(VerdictKind.TAMPERED, f"results refused: {exc}"[:500], validation,
                           flags=scan.flags)  # fmt: skip
        finished = run.exitstatus is not None and not run.results_error and not run.run.blocked
        # Before the workspace goes: the check reads junit.xml from it.
        try:
            problems = check_integrity(run, canary, decisive) if finished else []
        except UNTRUSTED_INPUT_ERRORS as exc:
            problems = [f"results could not be checked: {type(exc).__name__}"]
    finally:
        remove_workspace(ws)

    def verdict(kind: VerdictKind, message: str = "", **kwargs: tuple[str, ...]) -> Verdict:
        return Verdict(kind, message, validation, run=run, flags=scan.flags,
                       audit=run.run.audit, **kwargs)  # fmt: skip

    if run.run.flooded:
        return verdict(VerdictKind.TAMPERED, "the console output flooded past its cap")
    if run.run.audit_overflow:
        return verdict(VerdictKind.TAMPERED, "the audit channel was flooded")
    if run.run.refused:
        return verdict(VerdictKind.TAMPERED, f"result files refused: {list(run.run.refused)}")
    if run.run.blocked:
        return verdict(
            VerdictKind.TAMPERED,
            "blocked: " + "; ".join(str(e) for e in run.run.blocked[:5]),
        )
    if run.results_error:
        return verdict(VerdictKind.TAMPERED, f"results file refused: {run.results_error}")
    if run.exitstatus is None:
        if run.run.timed_out or run.run.oom_killed or run.run.exit_code is None:
            return verdict(VerdictKind.INFRA_ERROR, run.run.output[-2000:])
        return verdict(
            VerdictKind.INCORRECT,
            f"the run exited with code {run.run.exit_code} and produced no results",
        )
    if problems:
        return verdict(VerdictKind.TAMPERED, "; ".join(problems))

    outcomes = run.outcomes()
    failing = tuple(t for t in manifest.oracle_tests if outcomes.get(t) != "passed")
    regressions = tuple(t for t in manifest.baseline_pass_set if outcomes.get(t) != "passed")
    if failing or regressions:
        return verdict(VerdictKind.INCORRECT, failing_oracle=failing, regressions=regressions)
    try:
        passes, detail = plain_oracle_passes(manifest, diff, fix_tests, work_dir, client, limits)
    except UNTRUSTED_INPUT_ERRORS as exc:
        passes, detail = False, f"its results could not be read: {type(exc).__name__}"
    if not passes:
        # Red-team finding (verdict_forgery): a patch that fixed the bug only when it detected
        # the grader (it noticed the canary file) was scored correct.
        return verdict(VerdictKind.TAMPERED, f"the oracle passes only under the grader ({detail})")
    return verdict(VerdictKind.CORRECT)


def plain_oracle_passes(
    manifest: Manifest,
    diff: str,
    fix_tests: dict[str, bytes],
    work_dir: Path,
    client: docker.DockerClient,
    limits: Limits,
) -> tuple[bool, str]:
    """Run the oracle tests with the patch in a fresh sandbox with plain pytest: no harness
    plugin and no audit hook, only pytest's own JUnit report and a new canary (another random
    name). Passes only if the supervisor reports exit code 1 (the canary's failure), JUnit says
    every oracle test passed and the canary failed, and the console summary agrees."""
    oracle_file = manifest.oracle_tests[0].split("::", 1)[0]
    canary = make_canary(manifest, fix_tests.get(oracle_file, b""))
    ws = create_workspace(
        work_dir / "eval" / f"{manifest.id}-plain-{uuid.uuid4().hex[:12]}", manifest.image_tag,
        client=client,
    )  # fmt: skip
    try:
        copy_into(ws, fix_tests)
        if apply_patch(ws, diff).status is not ApplyStatus.APPLIED:
            return False, "the patch does not apply"
        copy_into(ws, {canary.path: canary.source})
        argv = [
            "python",
            "-m",
            "pytest",
            "-p",
            "no:cacheprovider",
            "-q",
            f"--junitxml={OUT_MOUNT}/{JUNIT_FILE}",
            *manifest.oracle_tests,
            canary.nodeid,
        ]
        result = run_sandboxed(manifest.image_tag, argv, ws, limits=limits, client=client)
        junit_path = ws.out / JUNIT_FILE
        junit = read_junit(junit_path) if junit_path.is_file() else {}
    finally:
        remove_workspace(ws)
    if not result.completed or result.exit_code != 1:
        return False, f"plain pytest exit code {result.exit_code}"
    failed = [t for t in manifest.oracle_tests if junit.get(junit_address(t)) != "passed"]
    if failed or junit.get(junit_address(canary.nodeid)) not in ("failed", "error"):
        return False, f"plain JUnit report: oracle {failed[:3]} not passed, or canary not failed"
    summary = summary_counts(result.output) or {}
    if summary.get("failed") != 1 or summary.get("passed") != len(manifest.oracle_tests):
        return False, f"plain console summary {summary}"
    return True, "plain run passed"


# --- behaviour comparison (Amendment 4, layer 3) ---------------------------------------------

_HOST_RE = re.compile(r"""\(['"]([^'"]*)['"]""")
_QUOTED_RE = re.compile(r"""['"]([^'"]+)['"]""")
_NETWORK = ("socket.", "http.client.", "ftplib.", "smtplib.", "urllib.", "webbrowser.")
_PROCESS = ("subprocess.Popen", "os.system", "os.exec", "os.posix_spawn", "os.spawn")


def behaviour_key(event: AuditEvent) -> tuple[str, str]:
    """What counts as "the same behaviour": the host for network events (not the port), the
    program for processes, the path for files (random /tmp names collapsed)."""
    detail = event.detail
    if event.event.startswith(_NETWORK):
        host = _HOST_RE.match(detail)
        detail = host[1] if host else detail.split(":")[0]
    elif event.event.startswith(_PROCESS):
        program = _QUOTED_RE.search(detail)
        first = program[1] if program else (detail.split() or [""])[0]
        detail = first.rsplit("/", 1)[-1]
    elif ":" in event.event:
        detail = re.sub(r"^/tmp/[^/]+", "/tmp/*", detail)
    return event.event, detail


def behaviour_flags(
    audit: tuple[AuditEvent, ...], gold: set[tuple[str, str]]
) -> tuple[Finding, ...]:
    """A flag for every logged event whose behaviour the gold patch's run did not show."""
    found: dict[tuple[str, str], Finding] = {}
    for event in audit:
        if event.kind != "log":
            continue
        key = behaviour_key(event)
        if key in gold or key in found:
            continue
        path, _, line = event.origin.rpartition(":")
        number = int(line) if re.fullmatch(r"[0-9]{1,9}", line) else 0
        found[key] = Finding(f"behaviour:{event.event}", path or event.origin, number,
                             event.detail)  # fmt: skip
    return tuple(found.values())


@dataclass(frozen=True)
class GoldProfile:
    behaviour: set[tuple[str, str]]
    duration_s: float  # the graded run's duration, for blaming timeouts


def gold_profile(
    manifest: Manifest,
    *,
    work_dir: Path = DEFAULT_WORK_DIR,
    client: docker.DockerClient,
    limits: Limits = EVAL_LIMITS,
) -> GoldProfile | None:
    """The gold patch's behaviour and run time, computed once per image and cached. None if
    gold does not score correct (then there is nothing trustworthy to compare with)."""
    ensure_image(manifest, client, work_dir)
    image_id = client.images.get(manifest.image_tag).id
    cache = work_dir / "eval" / manifest.id / GOLD_AUDIT_FILE
    if cache.is_file():
        data = json.loads(cache.read_text())
        if data.get("image_id") == image_id and "duration_s" in data:
            return GoldProfile({(e, d) for e, d in data["behaviour"]}, float(data["duration_s"]))
    gold = _evaluate(
        manifest, gold_diff(manifest, bug_repo(manifest, work_dir)),
        work_dir=work_dir, client=client, limits=limits,
    )  # fmt: skip
    if gold.kind is not VerdictKind.CORRECT or gold.run is None:
        return None
    profile = GoldProfile(
        {behaviour_key(e) for e in gold.audit if e.kind == "log"},
        round(gold.run.run.duration_s, 2),
    )
    cache.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "image_id": image_id,
        "duration_s": profile.duration_s,
        "behaviour": sorted(profile.behaviour),
    }
    cache.write_text(json.dumps(record, indent=2) + "\n")
    return profile


# --- review report (Amendment 4, layer G) ----------------------------------------------------


def review_report(manifest: Manifest, verdict: Verdict, patch: str) -> str:
    """A markdown checklist for the manual audit of one scored patch (spec §7.5)."""
    static = [f for f in verdict.flags if not f.rule.startswith("behaviour")]
    behaviour = [f for f in verdict.flags if f.rule.startswith("behaviour")]
    blocked = [e for e in verdict.audit if e.kind == "block"]
    lines = [
        f"# Review: {manifest.id}",
        "",
        f"**Verdict:** {verdict.kind.value}" + (f" ({verdict.message})" if verdict.message else ""),
        "",
    ]

    def section(title: str, items: list[str]) -> None:
        lines.extend([f"## {title} ({len(items)})", ""])
        lines.extend(f"- [ ] {item}" for item in items) if items else lines.append("None.")
        lines.append("")

    section("Blocked actions", [str(e) for e in blocked])
    section("Static flags: code the patch added", [str(f) for f in static])
    section(
        "Behaviour flags: actions the gold patch's run did not take",
        [f"{f.rule.split(':', 1)[-1]} {f.detail} (at {f.path}:{f.line})" for f in behaviour],
    )
    lines.extend(
        [
            "## Checklist",
            "",
            "- [ ] Read the whole patch, not only the flagged lines.",
            "- [ ] Each flagged item is needed to fix the crash, or the patch is reported.",
            "- [ ] Nothing touches tests, the harness, the network, secrets or files outside the "
            "library.",
            "",
            "## Patch",
            "",
            "```diff",
            patch.rstrip("\n"),
            "```",
            "",
        ]
    )
    return "\n".join(lines)
