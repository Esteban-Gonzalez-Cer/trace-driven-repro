"""Patch oracle (spec §7.3, §7.4, Amendment 4): score one patch for one bug in a fresh container.

1. Validate the diff on the host (spec §7.1). A violation is a Scope Violation.
2. Create a fresh workspace from `bug_env:{id}` (the `c_bug` snapshot). Nothing from any earlier
   run, including the agent's own container, is reused.
3. Copy the fix commit's test files in from `c_fix` (the held-out oracle).
4. Re-apply the patch from the host. A valid patch that does not apply is recorded as such.
5. Scan what the patch added (`sandbox.scanner`). Code that reaches for the test harness is a
   Scope Violation; other suspicious code is flagged for the reviewer.
6. Add a canary test, then run `oracle_tests` plus `baseline_pass_set` plus the canary. The
   plugin's audit hook blocks access to the results directory and logs suspicious actions.
7. Check the run's integrity (`evaluate.integrity`): a blocked action, an unreadable results
   file or an inconsistency between the results, JUnit, the console and the canary is Tampered.
8. Correct only if every oracle test passes and no baseline test fails.
9. Compare the run's audit events with the gold patch's; new ones are flagged for the reviewer.

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
from tracerepro.evaluate.integrity import check_integrity, make_canary
from tracerepro.sandbox.paths import is_test_path
from tracerepro.sandbox.runner import (
    AuditEvent,
    Limits,
    PytestRun,
    copy_into,
    create_workspace,
    remove_workspace,
    run_pytest,
)
from tracerepro.sandbox.scanner import Finding, scan_patch
from tracerepro.sandbox.validator import ApplyStatus, Validation, apply_patch, validate

EVAL_LIMITS = Limits(timeout_s=900)
GOLD_AUDIT_FILE = "gold-audit.json"


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
    profile = gold_profile(manifest, work_dir=work_dir, client=client, limits=limits)
    if profile is None:
        note = Finding("behaviour_unavailable", "", 0, "the gold patch did not score correct")
        return replace(verdict, flags=(*verdict.flags, note))
    return replace(verdict, flags=(*verdict.flags, *behaviour_flags(verdict.audit, profile)))


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
    canary = make_canary(manifest)
    decisive = [*manifest.oracle_tests, *manifest.baseline_pass_set]
    ws = create_workspace(
        work_dir / "eval" / f"{manifest.id}-{uuid.uuid4().hex[:12]}", manifest.image_tag,
        client=client,
    )  # fmt: skip
    try:
        copy_into(ws, fix_test_files(manifest, git_dir))
        applied = apply_patch(ws, diff)
        if applied.status is ApplyStatus.FAILED:
            return Verdict(VerdictKind.APPLY_FAILED, applied.message, validation)
        scan = scan_patch(ws.repo, validation.files)
        if not scan.ok:
            message = "; ".join(str(b) for b in scan.blocks)
            return Verdict(VerdictKind.SCOPE_VIOLATION, message, validation, flags=scan.flags)
        copy_into(ws, {canary.path: canary.source})
        run = run_pytest(
            manifest.image_tag, [*decisive, canary.nodeid], ws, limits=limits, client=client
        )
        finished = run.exitstatus is not None and not run.results_error and not run.run.blocked
        # Before the workspace goes: the check reads junit.xml from it.
        problems = check_integrity(run, canary, decisive) if finished else []
    finally:
        remove_workspace(ws)

    def verdict(kind: VerdictKind, message: str = "", **kwargs: tuple[str, ...]) -> Verdict:
        return Verdict(kind, message, validation, run=run, flags=scan.flags,
                       audit=run.run.audit, **kwargs)  # fmt: skip

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
    kind = VerdictKind.CORRECT if not failing and not regressions else VerdictKind.INCORRECT
    return verdict(kind, failing_oracle=failing, regressions=regressions)


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
        found[key] = Finding(
            f"behaviour:{event.event}",
            path or event.origin,
            int(line) if line.isdigit() else 0,
            event.detail,
        )
    return tuple(found.values())


def gold_profile(
    manifest: Manifest,
    *,
    work_dir: Path = DEFAULT_WORK_DIR,
    client: docker.DockerClient,
    limits: Limits = EVAL_LIMITS,
) -> set[tuple[str, str]] | None:
    """The gold patch's behaviour, computed once per image and cached. None if gold does not
    score correct (then there is nothing trustworthy to compare with)."""
    ensure_image(manifest, client, work_dir)
    image_id = client.images.get(manifest.image_tag).id
    cache = work_dir / "eval" / manifest.id / GOLD_AUDIT_FILE
    if cache.is_file():
        data = json.loads(cache.read_text())
        if data.get("image_id") == image_id:
            return {(e, d) for e, d in data["behaviour"]}
    gold = _evaluate(
        manifest, gold_diff(manifest, bug_repo(manifest, work_dir)),
        work_dir=work_dir, client=client, limits=limits,
    )  # fmt: skip
    if gold.kind is not VerdictKind.CORRECT:
        return None
    behaviour = {behaviour_key(e) for e in gold.audit if e.kind == "log"}
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(
        json.dumps({"image_id": image_id, "behaviour": sorted(behaviour)}, indent=2) + "\n"
    )
    return behaviour


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
