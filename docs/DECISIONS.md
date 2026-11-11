# Decisions

Choices left open by the spec, or packaging details that do not change it. Spec changes go in
[`AMENDMENTS.md`](AMENDMENTS.md) instead.

## 2026-11-01: Python versions

- **Harness:** Python 3.12 (`.python-version`, `requires-python >=3.12`, CI on 3.12), managed
  by uv with a committed `uv.lock`.
- **Bug environments:** each bug pins one exact Python version in its manifest
  (`python_version`), 3.11 by default (`python:3.11-slim`, spec §5.2). The harness never runs
  bug code in its own interpreter.

## 2026-11-01: Package namespace

Spec §11 lists `src/dataset/`, `src/agent/`, and so on. These live under a single package,
`src/tracerepro/<module>/`, so they cannot clash with top-level names such as `dataset`. The
module layout is unchanged.

## 2026-11-01: Spec tag name

The frozen spec is tagged `spec-v1.0` rather than `v1.0`, so harness release tags (`v0.x`,
`v1.x`) can never collide with it.

## 2026-11-01: Repository visibility

Public from day one, at `github.com/Esteban-Gonzalez-Cer/trace-driven-repro`.

## 2026-11-10: Context cap in characters, imports ranked by use

- The context cap (plan §6.2) is measured in **characters**, not tokens, so a second model would
  get byte-identical context whatever its tokenizer. Token counts are logged per call. The cap
  started at 120,000 characters and was raised to 250,000 on 2026-11-11 (see below).
- Imported files are ordered, and dropped over budget, by how often the trace files use the
  names they import from each file. An alphabetical order was tried first; on marshmallow-3082
  it dropped `fields.py`, the file the maintainers fixed, for no principled reason. Use counts
  come from the buggy code only and never look at the fix. Decided on the dev set, before any
  test-set bug is screened. Full rule in `METHODOLOGY.md`.
## 2026-11-10: Models and how they are reached (spec §16)

- **Main model:** Claude Sonnet 5.5 (`claude-sonnet-5-5`). Claude Haiku 5.5 (`claude-haiku-5-5`)
  is used only while testing the plumbing. Claude only for now; the client takes one provider
  per vendor, so a second vendor can be added later (spec §15 lists two models as frozen; until
  then the minimum tier, one model, applies).
- **Access through the Claude plan, not the paid API.** Every call goes through the `claude`
  CLI in script mode (`claude -p`), locked down to a plain text-in, text-out model (no tools,
  empty working folder, our own system prompt). No API key is used and no API money is spent.
  What is measured is therefore "the model as served by Claude Code in script mode, tools off";
  the CLI version is logged with every call (`LIMITATIONS.md`).
- **Settings recorded per run:** model id, effort and CLI version. There is no temperature:
  current Claude models accept only their default sampling settings, and the CLI does not
  expose it.
- **Output limit:** set through `CLAUDE_CODE_MAX_OUTPUT_TOKENS`. Checked with CLI 2.1.308: when
  an answer outgrows it, the CLI asks the model to continue up to four turns, then reports an
  error and drops the partial text. The harness counts that, and any reply that needed more
  than one turn, as output cut off (`max_tokens`): a failed attempt, never an infrastructure
  error. The real limit is therefore up to four times the setting; the default (16,000) is far
  above any patch the size limits allow.
- **Overhead:** the CLI adds about 590 prompt tokens of its own to every call (measured with
  `make llm-smoke`); they are counted in the logged usage and the per-run token cap.
- **Spending:** none on the API. Plan usage is limited by the usage guard (calls per hour and
  per day, a pause between calls) and by the per-run token cap.

## 2026-11-11: Feedback after any failed attempt; which patch is submitted

- The plan (§6.4) describes feedback when tests regress. A reply can also fail earlier: cut off,
  unreadable, rejected by the validator, or not applying. Each of these costs one attempt and is
  also followed by feedback saying what went wrong, so no attempt is wasted silently. The same
  rule holds for both pipelines.
- The submitted patch is the last one that ran clean on the visible tests, else the last one
  that applied. A patch that broke visible tests is still submitted when nothing better exists:
  the evaluator, not the agent, decides whether it is correct.

## 2026-11-11: Context cap raised to 250,000 characters; prompts v2

Found on the first dev run (PR #38): at 120,000 characters marshmallow-3082's fix lay in a file
shown only as an outline, and the model declined to guess. Decided with the project owner, on
the dev set, before any test-set bug is screened:

- **Cap: 250,000 characters** (about 60,000 tokens). Measured on the dev bugs: three fit whole
  (marshmallow-3004, -3016, -3082); flask-6096 keeps its trace file whole and outlines its
  imports; more-itertools-1342 keeps its 5,654-line trace file whole and drops its one import.
  The context covers the crash fix for all five (at 120,000: four of five).
- **Cost:** up to about twice the input tokens per call. Spec §9 estimated 10k to 30k tokens of
  context per call; that was an estimate, not a frozen value. The per-run token cap is raised
  provisionally from 150,000 to 300,000 to match, until step 6 measures real runs.
- **Prompts v2:** the system prompt now says that no more files will ever be shown, that some
  files may be shown only in part, and that the model should reason from what it has and still
  give its best answer; the retry prompt repeats it. v1 is kept for the earlier run's records.
- A readable reply with no fix in it is recorded as `declined`, not as a malformed reply.
- Fault Localization Miss is now checked line by line (Amendment 5).
- Not chosen: letting the model ask for more files. Spec §6 gives the agent static context with
  no repository search; that would change the research question and need an amendment that
  "it would be nicer" does not justify.

## 2026-11-11: Pipeline A details

- **Gate target from the payload:** the exception type (module-qualified as the harness plugin
  names it) and the innermost in-app frame of the payload. It equals the manifest's
  `target_exception` and `target_frame` by construction (Amendment 2), but the agent stage never
  reads the manifest's oracle fields.
- **Same rule as preflight:** the gate calls `dataset.preflight.crash_target`, so a failure
  raised in the test itself, or with no library frame, never counts as a repro.
- **One conversation:** the patch request follows the repro exchange in the same conversation,
  sending only the repro test and its failure (the context is already in the first turn).
- **The repro must pass after the patch.** A patch that applies with no regression but leaves
  the repro failing is a failed attempt (`repro_still_fails`); it can still be submitted if
  nothing better exists, and the evaluator decides.
- **Run status without a valid repro:** `no_repro` when the last test passed on the buggy code,
  `invalid_repro` otherwise; each attempt keeps its own label for the taxonomy.

## 2026-11-11: Measured usage; per-run token cap and pacing (plan §6.6)

Both pipelines on the 5 dev bugs with Claude Sonnet 5.5, cache off, one trial, no cap in the
way (run `measure-e90962f5-v2-claude-sonnet-5-5-cli2.1.308`). Tokens count every input, output
and cached token of every call; dollars are the CLI's list-price estimate (nothing is spent on
the plan).

| bug | A tokens | A calls | B tokens | B calls |
|---|---|---|---|---|
| flask-6096 | 137,679 | 2 | 68,360 | 1 |
| marshmallow-3004 | 146,796 | 2 | 73,097 | 1 |
| marshmallow-3016 | 30,577 | 2 | 15,245 | 1 |
| marshmallow-3082 | 143,293 | 2 | 71,451 | 1 |
| more-itertools-1342 | 269,425 | 3 | 88,891 | 1 |
| **mean** | **145,554** (about $0.59) | 2.2 | **63,408** (about $0.26) | 1.0 |

Agent time per run: about 23 s (A) and 14 s (B); grading takes longer than the agent.

- **Per-run token cap: 400,000**, shared by both pipelines (spec §6 budget matching). Each call
  re-sends the whole conversation, so a run costs about one context per call; Pipeline A's worst
  case (2 repro and 2 patch calls) on the largest dev context is about 360,000. The cap never
  cut a measured run short; it exists to stop a runaway one.
- **Pacing** stays at 60 calls per hour, 400 per day, 2 s between calls. One model's full
  experiment (30 bugs, 3 trials, 2 pipelines) is about 90 runs per pipeline, roughly 290 calls
  and 19 million tokens (about $77 at list price): a day or two of plan usage.
- **Spending limit:** none in money, since the plan is used. The guard and the cap are the
  limits.

## 2026-11-11: Safety refusals are model refusals

When the model's safety classifier declines a request, the CLI reports an error ("safeguards
flagged this message") instead of a `refusal` stop reason. It is classified as a refusal: a
failed attempt, never retried, never an infrastructure failure (which would make the runner set
the trial aside and rerun it indefinitely). Found by the prompt-injection round.

## 2026-11-11: One model (Claude); Codex tested and set aside

Decided by the project owner. The study uses one model, Claude Sonnet 5.5 through the Claude
plan (the spec's minimum tier, §2). A second vendor was considered only if it could also run on
a subscription plan, so OpenAI's Codex CLI (0.167.0-alpha, bundled with the ChatGPT app, signed
in with the owner's ChatGPT account) was tested with six small calls from an empty folder:

- It runs in script mode (`codex exec --json`), reports token usage, and the model can be chosen.
- With its defaults, asked to, it read a decoy file in its folder (the secret came back in the
  reply), ran `ls`, and browsed a web page, all in its "read-only" sandbox.
- The shell and web search can be switched off; image viewing, patch writing, image generation,
  helper agents and a code runner cannot. Its built-in instructions (about 13,800 tokens per
  call) cannot be replaced (`base_instructions` is ignored), only added to.

Containment would be weaker than for Claude (tools would run before a reply could be refused),
and the comparison would mix the model with Codex's own agent instructions. Revisit if a later
release can switch off every tool and replace its instructions, or with an API key.

## 2026-11-11: Bugs in scope, and their kind

Decided by the project owner: what matters is a bug that surfaces as an exception with a stack
trace, whether or not the application keeps running. A backend exception that is caught and
logged still leaves a traceback (exactly what an error tracker captures) and can still break a
feature, such as logging in.

- **In scope:** every bug whose regression test fails with an uncaught exception from library
  code, as selection criterion 1 already says. This keeps marshmallow-3016 and -3082, whose
  library raises its own `ValidationError` for valid input. No amendment is needed.
- **Out of scope:** bugs with no exception at all (silently wrong output): there is no trace for
  the agent to start from.
- **Kind, recorded per bug** (`crash_kind` in the manifest, set by preflight from the innermost
  frame of the oracle failure): `library_raise` when that frame is in the library and its line
  is a `raise` statement (the library rejected something on purpose); `python_failure`
  otherwise (Python or the standard library failed inside a library call). Dev set: flask-6096,
  marshmallow-3004, more-itertools-1342 are `python_failure`; marshmallow-3016 and -3082 are
  `library_raise`.
- **Reporting:** results are also given per kind, descriptively (the groups are small).
- **Test-set balance:** at most 6 test bugs per library, so that no library dominates (3 of the
  5 dev bugs are marshmallow).

## Open (spec §16)

- Real deadline (the plan assumes 12 weeks from 2026-11-01).
- Where the write-up is published.
- Final repository list after early screening.
