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
- **Failed attempts:** a reply cut off or refused, a reply with no fix in it (`declined`), a
  broken output contract, a patch the validator rejects, one that does not apply, or one that
  breaks visible tests. Each costs an attempt and is recorded with its label.
- **Not the model's fault:** `InfraFailure`, `UsageLimitReached` and `PacingLimitReached`
  propagate to the runner, which reruns or resumes the trial; they are never recorded as a
  model result.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass, field
from typing import Any

from tracerepro.agent.client import LLMClient, Message, Request, Settings
from tracerepro.agent.edits import EditResult, apply_edits
from tracerepro.agent.errors import TokenCapReached
from tracerepro.agent.parse import (
    SCOPE_VIOLATION,
    ContractFailure,
    Failure,
    parse_edits,
    parse_repro,
)
from tracerepro.agent.prompts import (
    CURRENT_VERSION,
    PATCH,
    REPRO,
    REPRO_FEEDBACK,
    SYSTEM,
    DataBlock,
    Prompt,
    build_message,
    leak_check,
    load_prompt,
    patch_message,
    repro_message,
)
from tracerepro.agent.repro_gate import (
    NO_REPRO,
    REPRO_PATH,
    VALID,
    GateResult,
    Target,
    judge,
    payload_target,
    repro_passes,
)
from tracerepro.agent.usage_guard import TokenBudget
from tracerepro.agent.workbench import TrialRun, Workbench
from tracerepro.context.assembler import DEFAULT_BUDGET_CHARS, Context, assemble
from tracerepro.sandbox.validator import validate

FEEDBACK = "feedback"
REGRESSION_FAILURE = "regression_failure"
MODEL_FAILURE = "model_failure"
DECLINED = "declined"  # a readable reply with no fix in it


@dataclass(frozen=True)
class PipelineConfig:
    settings: Settings
    prompt_version: str = CURRENT_VERSION
    max_patch_attempts: int = 2
    max_repro_attempts: int = 2
    # Appended to the system prompt. Empty in every benchmark run; the prompt-injection referee
    # uses it to hide a canary sentence it then looks for in the replies.
    system_suffix: str = ""
    token_cap: int = 300_000
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
            system=self.system.text + self.config.system_suffix,
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
    if isinstance(edits, ContractFailure) and edits.kind is Failure.NO_EDIT_BLOCK:
        # A readable reply with no fix at all: the model declined (for example because code it
        # wanted was not shown). Still a failed attempt, but not a broken output format.
        attempt.outcome, attempt.label = DECLINED, DECLINED
        attempt.detail = "the reply contains no edit block"
        return attempt, None
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
    if attempt.outcome == DECLINED:
        return (
            "Your reply contained no edit block, so nothing was tried. No more files can be shown; "
            "give your best fix from the code you have."
        )
    return f"Your reply could not be used: {attempt.detail}"


def best_patch(attempts: Sequence[Attempt]) -> Attempt | None:
    """The last attempt that ran clean, else the last whose patch applied."""
    patches = [a for a in attempts if a.stage == "patch" and a.diff is not None]
    for wanted in (("clean",), ("clean", "regressions", "no_results", "repro_still_fails")):
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
    accept: Callable[[TrialRun], str | None] | None = None,
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


def _repro_attempt(
    n: int, reply: str, stop: str, tokens: int, bench: Workbench, target: Target
) -> tuple[Attempt, GateResult | None, str | None]:
    attempt = Attempt(n=n, stage="repro", outcome="", stop_reason=stop, reply=reply, tokens=tokens)
    if stop in ("max_tokens", "refusal"):
        attempt.outcome, attempt.label = MODEL_FAILURE, MODEL_FAILURE
        attempt.detail = "the reply was cut off" if stop == "max_tokens" else "the model refused"
        return attempt, None, None
    test = parse_repro(reply)
    if isinstance(test, ContractFailure):
        declined = test.kind is Failure.NO_CODE_BLOCK
        attempt.outcome = DECLINED if declined else "contract_failure"
        attempt.label = DECLINED if declined else test.label
        attempt.detail = str(test)
        return attempt, None, None
    trial = bench.try_patch("", extra_files={REPRO_PATH: test.encode()}, tests=[REPRO_PATH],
                            baseline=False)  # fmt: skip
    gate = judge(trial, target)
    attempt.outcome, attempt.label, attempt.detail = gate.verdict, gate.label, gate.detail
    return attempt, gate, test


def _repro_feedback(attempt: Attempt, gate: GateResult | None) -> str:
    if gate is not None:
        return gate.report
    if attempt.outcome == DECLINED:
        return "Your reply contained no Python code block, so nothing was run."
    return f"Your reply could not be used: {attempt.detail}"


def run_pipeline_a(
    bug: Bug, client: LLMClient, bench: Workbench, config: PipelineConfig
) -> AgentResult:
    """Pipeline A (repro first, plan §6.5): a repro test that passes the gate (up to
    `max_repro_attempts` replies), then patches that must make it pass without regressions. No
    valid repro means the run stops: `no_repro` or `invalid_repro` (the strict rule, spec §6)."""
    session = _Session(client, config)
    context = context_for(bug, bench, config)
    version = config.prompt_version
    repro_prompt, retry_prompt = load_prompt(REPRO, version), load_prompt(REPRO_FEEDBACK, version)
    patch_prompt = load_prompt(PATCH, version)
    result = AgentResult(pipeline="A", status="", context=context.summary())
    result.prompts = {
        p.id: p.sha256
        for p in (session.system, repro_prompt, retry_prompt, patch_prompt,
                  load_prompt(FEEDBACK, version))
    }  # fmt: skip
    target = payload_target(bug.payload)
    message = repro_message(repro_prompt, payload=bug.payload_text, context=context.render())
    text, tags, prompt = message.text, list(message.tags), repro_prompt
    gate: GateResult | None = None
    for n in range(1, config.max_repro_attempts + 1):
        try:
            reply, stop, tokens = session.ask(prompt, text, tags)
        except TokenCapReached:
            result.stopped_by = "token_cap"
            break
        attempt, judged, test = _repro_attempt(n, reply, stop, tokens, bench, target)
        result.attempts.append(attempt)
        if judged is not None and judged.verdict == VALID and test is not None:
            gate, result.repro = judged, test
            break
        if n < config.max_repro_attempts:
            attempt.feedback = _repro_feedback(attempt, judged)
            follow = build_message(retry_prompt.text, [DataBlock("repro-result", attempt.feedback)])
            text, tags, prompt = follow.text, list(follow.tags), retry_prompt
    if gate is None or result.repro is None:
        last = result.attempts[-1].outcome if result.attempts else ""
        result.status = "no_repro" if last == NO_REPRO else "invalid_repro"
        return _finish(session, result)
    # Patch stage: the valid repro and its failure go in the same conversation.
    ask = build_message(
        patch_prompt.text,
        [DataBlock("repro-test", result.repro), DataBlock("repro-result", gate.report)],
    )
    patch_loop(
        session,
        result,
        bench,
        (ask.text, list(ask.tags)),
        extra_files={REPRO_PATH: result.repro.encode()},
        tests=[REPRO_PATH],
        accept=repro_passes,
    )
    return _finish(session, result)
