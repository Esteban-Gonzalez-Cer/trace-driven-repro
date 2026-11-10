"""Patch oracle (spec §7.3, §7.4): score one patch for one bug in a fresh container.

1. Validate the diff on the host (spec §7.1). A violation is a Scope Violation.
2. Create a fresh workspace from `bug_env:{id}` (the `c_bug` snapshot). Nothing from any earlier
   run, including the agent's own container, is reused.
3. Copy the fix commit's test files in from `c_fix` (the held-out oracle).
4. Re-apply the patch from the host. A valid patch that does not apply is recorded as such.
5. Run `oracle_tests` plus `baseline_pass_set`. Correct only if every oracle test passes and no
   baseline test fails.

This is the core of the Phase 3 evaluator; the failure taxonomy builds on its verdicts.
"""

from __future__ import annotations

import enum
import uuid
from dataclasses import dataclass, field
from pathlib import Path

import docker
import docker.errors

from tracerepro.dataset.image_builder import DEFAULT_WORK_DIR, build_image
from tracerepro.dataset.manifest import Manifest
from tracerepro.dataset.repo import changed_files, clone_name, diff, ensure_clone, show_file
from tracerepro.sandbox.paths import is_test_path
from tracerepro.sandbox.runner import (
    Limits,
    PytestRun,
    copy_into,
    create_workspace,
    remove_workspace,
    run_pytest,
)
from tracerepro.sandbox.validator import ApplyStatus, Validation, apply_patch, validate

EVAL_LIMITS = Limits(timeout_s=900)


class VerdictKind(enum.StrEnum):
    CORRECT = "correct"
    INCORRECT = "incorrect"
    SCOPE_VIOLATION = "scope_violation"
    APPLY_FAILED = "apply_failed"
    INFRA_ERROR = "infra_error"  # the run did not finish; never a model failure


@dataclass(frozen=True)
class Verdict:
    kind: VerdictKind
    message: str = ""
    validation: Validation | None = None
    failing_oracle: tuple[str, ...] = ()
    regressions: tuple[str, ...] = ()
    run: PytestRun | None = field(default=None, compare=False)


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
    if not manifest.oracle_tests:
        raise ValueError(f"{manifest.id}: manifest has no oracle_tests; run preflight first")
    validation = validate(diff)
    if not validation.ok:
        summary = "; ".join(
            f"{v.rule}: {v.path} {v.message}".strip() for v in validation.violations
        )
        return Verdict(VerdictKind.SCOPE_VIOLATION, summary, validation)

    client = client or docker.from_env()
    ensure_image(manifest, client, work_dir)
    git_dir = bug_repo(manifest, work_dir)
    ws = create_workspace(
        work_dir / "eval" / f"{manifest.id}-{uuid.uuid4().hex[:12]}", manifest.image_tag,
        client=client,
    )  # fmt: skip
    try:
        copy_into(ws, fix_test_files(manifest, git_dir))
        applied = apply_patch(ws, diff)
        if applied.status is ApplyStatus.FAILED:
            return Verdict(VerdictKind.APPLY_FAILED, applied.message, validation)
        run = run_pytest(
            manifest.image_tag,
            [*manifest.oracle_tests, *manifest.baseline_pass_set],
            ws,
            limits=limits,
            client=client,
        )
    finally:
        remove_workspace(ws)

    if run.exitstatus is None:
        return Verdict(VerdictKind.INFRA_ERROR, run.run.output[-2000:], validation, run=run)
    outcomes = run.outcomes()
    failing = tuple(t for t in manifest.oracle_tests if outcomes.get(t) != "passed")
    regressions = tuple(t for t in manifest.baseline_pass_set if outcomes.get(t) != "passed")
    kind = VerdictKind.CORRECT if not failing and not regressions else VerdictKind.INCORRECT
    return Verdict(kind, "", validation, failing, regressions, run)
