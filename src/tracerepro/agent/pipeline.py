"""The agent pipelines (development plan §6.4 and §6.5, spec §6).

**Pipeline B (direct patch).** Send the crash report and the context; ask for a patch. Parse the
edit blocks, turn them into a diff, check it with the validator, and try it against the visible
tests on the buggy code. If something went wrong and an attempt remains, send back what
happened (the failing test names and the last 60 lines of the run, or why the reply could not
be used) and ask for a revised patch. At most `max_patch_attempts` patch replies.

Both pipelines follow the same rules, so the comparison is fair:

- **What the agent sees:** only the buggy code, its own work and the visible tests' results
  (the workbench never has fix-commit files). This module never imports the evaluator.
- **Budget:** one token cap per run, counting every token of every call. When it is reached the
  run stops and submits its best patch: the last one that ran clean, else the last one that
  applied.
- **Failed attempts:** a reply cut off or refused, a broken output contract, a patch the
  validator rejects, one that does not apply, or one that breaks visible tests. Each costs an
  attempt and is recorded with its taxonomy label.
- **Not the model's fault:** `InfraFailure`, `UsageLimitReached` and `PacingLimitReached`
  propagate to the runner, which reruns or resumes the trial; they are never recorded as a
  model result.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from typing import Any

from tracerepro.agent.client import LLMClient, Message, Request, Settings
from tracerepro.agent.edits import EditResult, apply_edits
from tracerepro.agent.errors import TokenCapReached
from tracerepro.agent.parse import SCOPE_VIOLATION, ContractFailure, parse_edits
from tracerepro.agent.prompts import (
    PATCH,
    SYSTEM,
    DataBlock,
    Prompt,
    build_message,
    leak_check,
    load_prompt,
    patch_message,
)
from tracerepro.agent.usage_guard import TokenBudget
from tracerepro.agent.workbench import TrialRun, Workbench
from tracerepro.context.assembler import DEFAULT_BUDGET_CHARS, Context, assemble
from tracerepro.sandbox.validator import validate

FEEDBACK = "feedback"
REGRESSION_FAILURE = "regression_failure"
MODEL_FAILURE = "model_failure"


@dataclass(frozen=True)
class PipelineConfig:
    settings: Settings
    prompt_version: str = "v1"
    max_patch_attempts: int = 2
    token_cap: int = 150_000
    context_budget: int = DEFAULT_BUDGET_CHARS


@dataclass
class Attempt:
    n: int
    stage: str  # "patch" or "repro"
    outcome: str  # clean, regressions, no_results, contract_failure, scope_violation, ...
    label: str | None = None  # failure taxonomy category for a failed attempt
    detail: str = ""
    stop_reason: str = ""
    reply: str = ""
    diff: str | None = None
    regressions: tuple[str, ...] = ()
    feedback: str | None = None  # what was sent back afterwards
    tokens: int = 0


@dataclass
class AgentResult:
    pipeline: str
    status: str  # submitted, no_patch, no_repro, invalid_repro
    final_diff: str | None = None
    final_from: int | None = None  # attempt number the submitted patch came from
    stopped_by: str | None = None  # "token_cap" when the budget ended the run
    attempts: list[Attempt] = field(default_factory=list)
    tokens_used: int = 0
    prompt_leaks: list[str] = field(default_factory=list)
    context: dict[str, Any] = field(default_factory=dict)
    prompts: dict[str, str] = field(default_factory=dict)  # prompt id -> sha256
    repro: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class Bug:
    """What the agent stage knows about a bug: its id, the payload and the visible code."""

    id: str
    payload: dict[str, Any]

    @property
    def payload_text(self) -> str:
        return json.dumps(self.payload, indent=2, sort_keys=True)


def describe_trial(trial: TrialRun) -> str:
    """The test-results feedback: failing visible tests and the end of the run's output."""
    if not trial.applied:
        return f"The patch could not be applied to the code:\n{trial.message}"
    if not trial.finished:
        return f"{trial.message}.\n\nLast lines of the output:\n{trial.output_tail}"
    lines = [f"The patch applied, but {len(trial.regressions)} visible tests that passed before "
             "now fail:"]  # fmt: skip
    lines += [f"- {t}" for t in trial.regressions[:30]]
    if len(trial.regressions) > 30:
        lines.append(f"- ... and {len(trial.regressions) - 30} more")
    lines += ["", "Last lines of the test run output:", trial.output_tail]
    return "\n".join(lines)


class _Session:
    """One run's conversation with the model, its budget and its leak checks."""

    def __init__(self, client: LLMClient, config: PipelineConfig) -> None:
        self.client = client
        self.config = config
        self.system = load_prompt(SYSTEM, config.prompt_version)
        self.budget = TokenBudget(config.token_cap)
        self.messages: list[Message] = []
        self.tags: list[str] = []
        self.leaks: list[str] = []

    def ask(self, prompt: Prompt, text: str, tags: Sequence[str]) -> tuple[str, str, int]:
        """Send `text` as the next user turn; return (reply, stop reason, tokens used)."""
        self.messages.append(Message("user", text))
        self.tags.extend(tags)
        request = Request(
            system=self.system.text,
            messages=tuple(self.messages),
            settings=self.config.settings,
            prompt_version=prompt.id,
            prompt_hash=prompt.sha256,
        )
        before = self.budget.used
        completion = self.client.complete(request, self.budget)
        self.messages.append(Message("assistant", completion.text))
        hits = leak_check(completion.text, [self.system], self.tags)
        self.leaks += [f"call {len(self.messages) // 2}: {hit}" for hit in hits]
        return completion.text, completion.stop_reason, self.budget.used - before

    def feedback(self, text: str) -> tuple[str, list[str]]:
        prompt = load_prompt(FEEDBACK, self.config.prompt_version)
        message = build_message(prompt.text, [DataBlock("test-results", text)])
        return message.text, list(message.tags)


def _patch_attempt(
    n: int, reply: str, stop: str, tokens: int, bench: Workbench, extra: dict[str, bytes],
    tests: Sequence[str],
) -> tuple[Attempt, TrialRun | None]:  # fmt: skip
    attempt = Attempt(n=n, stage="patch", outcome="", stop_reason=stop, reply=reply, tokens=tokens)
    if stop in ("max_tokens", "refusal"):
        attempt.outcome, attempt.label = MODEL_FAILURE, MODEL_FAILURE
        attempt.detail = "the reply was cut off" if stop == "max_tokens" else "the model refused"
        return attempt, None
    edits = parse_edits(reply)
    if isinstance(edits, ContractFailure):
        attempt.outcome, attempt.label, attempt.detail = "contract_failure", edits.label, str(edits)
        return attempt, None
    made = apply_edits(bench.source_tree(), edits)
    if isinstance(made, ContractFailure):
        attempt.outcome, attempt.label, attempt.detail = "contract_failure", made.label, str(made)
        return attempt, None
    assert isinstance(made, EditResult)
    attempt.diff = made.diff
    validation = validate(made.diff)
    if not validation.ok:
        attempt.outcome, attempt.label = SCOPE_VIOLATION, SCOPE_VIOLATION
        attempt.detail = "; ".join(f"{v.rule}: {v.path} {v.message}".strip()
                                   for v in validation.violations)  # fmt: skip
        return attempt, None
    trial = bench.try_patch(made.diff, extra_files=extra, tests=tests)
    if not trial.applied:
        attempt.outcome, attempt.label, attempt.detail = (
            "apply_failed",
            "syntax_or_import_error",
            (trial.message),
        )
    elif not trial.finished:
        attempt.outcome, attempt.label, attempt.detail = (
            "no_results",
            REGRESSION_FAILURE,
            (trial.message),
        )
    elif trial.regressions:
        attempt.outcome, attempt.label = "regressions", REGRESSION_FAILURE
        attempt.regressions = trial.regressions
        attempt.detail = f"{len(trial.regressions)} visible tests now fail"
    else:
        attempt.outcome = "clean"
    return attempt, trial


def _feedback_text(attempt: Attempt, trial: TrialRun | None) -> str:
    if trial is not None:
        return describe_trial(trial)
    if attempt.outcome == MODEL_FAILURE:
        return f"Your reply could not be used: {attempt.detail}. Keep the answer short."
    if attempt.outcome == SCOPE_VIOLATION:
        return f"The patch was rejected before running: {attempt.detail}"
    return f"Your reply could not be used: {attempt.detail}"


def best_patch(attempts: Sequence[Attempt]) -> Attempt | None:
    """The last attempt that ran clean, else the last whose patch applied."""
    patches = [a for a in attempts if a.stage == "patch" and a.diff is not None]
    for wanted in (("clean",), ("clean", "regressions", "no_results")):
        found = [a for a in patches if a.outcome in wanted]
        if found:
            return found[-1]
    return None


def patch_loop(
    session: _Session,
    result: AgentResult,
    bench: Workbench,
    first_message: tuple[str, list[str]],
    *,
    extra_files: dict[str, bytes] | None = None,
    tests: Sequence[str] = (),
    accept: Any = None,
) -> None:
    """Ask for patches until one is accepted or the attempts run out. `accept(trial)` may add a
    condition (Pipeline A: the repro test must pass); it returns None or a reason to retry."""
    patch_prompt = load_prompt(PATCH, session.config.prompt_version)
    text, tags = first_message
    prompt = patch_prompt
    for n in range(1, session.config.max_patch_attempts + 1):
        try:
            reply, stop, tokens = session.ask(prompt, text, tags)
        except TokenCapReached:
            result.stopped_by = "token_cap"
            break
        attempt, trial = _patch_attempt(n, reply, stop, tokens, bench, extra_files or {}, tests)
        if attempt.outcome == "clean" and accept is not None and trial is not None:
            reason = accept(trial)
            if reason:
                attempt.outcome, attempt.label, attempt.detail = (
                    "repro_still_fails",
                    ("incorrect_patch"),
                    reason,
                )
        result.attempts.append(attempt)
        if attempt.outcome == "clean":
            break
        if n < session.config.max_patch_attempts:
            body = _feedback_text(attempt, trial)
            if attempt.outcome == "repro_still_fails":
                body = f"{attempt.detail}\n\n{body}"
            attempt.feedback = body
            text, tags = session.feedback(body)
            prompt = load_prompt(FEEDBACK, session.config.prompt_version)


def _finish(session: _Session, result: AgentResult) -> AgentResult:
    chosen = best_patch(result.attempts)
    if chosen is not None:
        result.final_diff, result.final_from = chosen.diff, chosen.n
        result.status = "submitted"
    elif result.status == "":
        result.status = "no_patch"
    result.tokens_used = session.budget.used
    result.prompt_leaks = session.leaks
    return result


def context_for(bug: Bug, bench: Workbench, config: PipelineConfig) -> Context:
    return assemble(bug.payload, bench.source_tree(), budget=config.context_budget)


def run_pipeline_b(
    bug: Bug, client: LLMClient, bench: Workbench, config: PipelineConfig
) -> AgentResult:
    session = _Session(client, config)
    context = context_for(bug, bench, config)
    patch_prompt = load_prompt(PATCH, config.prompt_version)
    result = AgentResult(pipeline="B", status="", context=context.summary())
    result.prompts = {
        p.id: p.sha256
        for p in (session.system, patch_prompt, load_prompt(FEEDBACK, config.prompt_version))
    }
    message = patch_message(patch_prompt, payload=bug.payload_text, context=context.render())
    patch_loop(session, result, bench, (message.text, list(message.tags)))
    return _finish(session, result)
