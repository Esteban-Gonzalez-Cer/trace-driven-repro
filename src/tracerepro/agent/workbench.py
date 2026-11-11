"""Where the agent tries its work at agent time (spec §7.3): the buggy code and its visible tests.

A `Workbench` gives the pipelines the `c_bug` source tree (for the context and the edits) and
runs a candidate patch, plus optional extra files such as a repro test, against the **visible**
tests, in the same locked-down sandbox the evaluator uses.

What the agent can learn here is limited on purpose (the feedback rule, plan §6.5):

- the tree comes from the `bug_env:{id}` image, built from `c_bug`, which holds no fix-commit
  file, so no oracle test exists in it;
- only the manifest's baseline tests are run, from the test files that exist at `c_bug`. Baseline
  tests that live only in fix-commit files are simply absent;
- nothing here imports the evaluator.

A regression is a baseline test that did not pass, or that sat in a test file that could no
longer be collected.
"""

from __future__ import annotations

import shutil
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from types import TracebackType
from typing import Protocol

import docker
import docker.errors

from tracerepro.dataset.image_builder import DEFAULT_WORK_DIR, build_image
from tracerepro.dataset.manifest import Manifest
from tracerepro.sandbox.runner import (
    Limits,
    PytestRun,
    Workspace,
    copy_into,
    create_workspace,
    remove_workspace,
    run_pytest,
)
from tracerepro.sandbox.validator import ApplyStatus, apply_patch

AGENT_LIMITS = Limits(timeout_s=600)
OUTPUT_TAIL_LINES = 60


@dataclass(frozen=True)
class TrialRun:
    applied: bool
    message: str = ""  # why the patch did not apply, or what went wrong with the run
    regressions: tuple[str, ...] = ()
    passed: int = 0
    finished: bool = True  # False: the run produced no results
    infra: bool = False  # the sandbox itself failed (timeout of a healthy run, Docker error)
    output_tail: str = ""
    duration_s: float = 0.0
    run: PytestRun | None = field(default=None, compare=False, repr=False)

    @property
    def clean(self) -> bool:
        return self.applied and self.finished and not self.regressions


class Workbench(Protocol):
    def source_tree(self) -> Path: ...

    def try_patch(
        self,
        diff: str,
        *,
        extra_files: Mapping[str, bytes] = ...,
        tests: Sequence[str] = ...,
        baseline: bool = ...,
    ) -> TrialRun: ...


def tail(text: str, lines: int = OUTPUT_TAIL_LINES) -> str:
    return "\n".join(text.rstrip("\n").split("\n")[-lines:])


def regressions_of(run: PytestRun, baseline: Sequence[str]) -> tuple[str, ...]:
    """Baseline tests that ran and did not pass, or whose file failed to collect."""
    outcomes = run.outcomes()
    broken_files = {
        nodeid.split("::", 1)[0]
        for nodeid, outcome in outcomes.items()
        if "::" not in nodeid and outcome in ("error", "failed")
    }
    found = []
    for test in baseline:
        if test.split("::", 1)[0] in broken_files or outcomes.get(test, "passed") != "passed":
            found.append(test)
    return tuple(found)


class DockerWorkbench:
    """The real workbench: one pristine copy of the image's `/repo`, and a fresh copy of it for
    every attempt. Use as a context manager; everything is removed on exit."""

    def __init__(
        self,
        manifest: Manifest,
        root: Path,
        *,
        client: docker.DockerClient | None = None,
        limits: Limits = AGENT_LIMITS,
        work_dir: Path = DEFAULT_WORK_DIR,
    ) -> None:
        self.manifest = manifest
        self.root = root
        self.client = client or docker.from_env()
        self.limits = limits
        self.work_dir = work_dir
        self._pristine: Workspace | None = None
        self._count = 0

    def __enter__(self) -> DockerWorkbench:
        try:
            self.client.images.get(self.manifest.image_tag)
        except docker.errors.ImageNotFound:
            build_image(self.manifest, work_dir=self.work_dir, client=self.client)
        self.root.mkdir(parents=True, exist_ok=True)
        self._pristine = create_workspace(
            self.root / "pristine", self.manifest.image_tag, client=self.client
        )
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    def source_tree(self) -> Path:
        if self._pristine is None:
            raise RuntimeError("use the workbench as a context manager")
        return self._pristine.repo

    def visible_baseline(self) -> tuple[list[str], list[str]]:
        """(test files to run, baseline test ids) restricted to files present at `c_bug`."""
        tree = self.source_tree()
        named = {t.split("::", 1)[0] for t in self.manifest.baseline_pass_set}
        files = sorted(f for f in named if (tree / f).is_file() and not (tree / f).is_symlink())
        tests = [t for t in self.manifest.baseline_pass_set if t.split("::", 1)[0] in files]
        return files, tests

    def try_patch(
        self,
        diff: str,
        *,
        extra_files: Mapping[str, bytes] | None = None,
        tests: Sequence[str] = (),
        baseline: bool = True,
    ) -> TrialRun:
        """Apply `diff` (may be empty) to a fresh copy, add `extra_files`, and run `tests` plus,
        with `baseline`, the visible baseline."""
        if not baseline and not tests:
            raise ValueError("nothing to run: no tests and no baseline")
        self._count += 1
        ws = Workspace(self.root / f"try-{self._count}-{uuid.uuid4().hex[:8]}")
        ws.root.mkdir(parents=True)
        ws.out.mkdir()
        shutil.copytree(self.source_tree(), ws.repo, symlinks=True)
        try:
            if extra_files:
                copy_into(ws, dict(extra_files))
            applied = apply_patch(ws, diff)
            if applied.status is not ApplyStatus.APPLIED:
                return TrialRun(applied=False, message=applied.message)
            files, visible = self.visible_baseline() if baseline else ([], [])
            run = run_pytest(
                self.manifest.image_tag,
                [*files, *tests],
                ws,
                limits=self.limits,
                client=self.client,
            )
        finally:
            remove_workspace(ws)
        output = tail(run.run.output)
        if run.exitstatus is None:
            killed = run.run.timed_out or run.run.oom_killed or run.run.exit_code is None
            reason = (
                "the test run was stopped (time or memory limit)"
                if killed
                else f"the test run exited with code {run.run.exit_code} and produced no results"
            )
            return TrialRun(
                applied=True,
                message=reason,
                finished=False,
                infra=False,
                output_tail=output,
                duration_s=run.run.duration_s,
                run=run,
            )
        outcomes = run.outcomes()
        return TrialRun(
            applied=True,
            regressions=regressions_of(run, visible),
            passed=sum(1 for t in visible if outcomes.get(t) == "passed"),
            output_tail=output,
            duration_s=run.run.duration_s,
            run=run,
        )
