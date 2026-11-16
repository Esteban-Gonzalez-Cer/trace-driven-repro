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

## 2026-11-11: Screening order by crash words

Candidate pull requests are found mechanically (`find_candidates`), and those whose title or
description mentions an exception, a traceback or a crash are screened first. Mechanical
screening of every merged pull request would mostly test feature work: the first 25 candidates
had a 20% yield even when picked by hand, and each failing candidate costs an image build. The
order changes which candidates are screened first, never whether a screened candidate passes,
and every screened candidate is logged. Possible effect, noted for LIMITATIONS.md: the test set
leans toward bugs whose fixes were described as crashes.

## 2026-11-11: Environment fixes found in the first screening batch

Tooling fixes, applied to every candidate; none changes a selection criterion or a check.
Candidates that failed only because of them were screened again.

- **Real version instead of `0.0.0`.** Images install the project with the version of the newest
  release tag reachable from `c_bug` (`repo.tag_version`; `0.0.0` only if there is none). With
  `0.0.0`, `pip check` failed when a test dependency needs a minimum version of the project
  itself (attrs-1571: hypothesis needs attrs>=22.2).
- **Commits older than the project's `uv.lock`.** The `uv-lock` profile then resolves the same
  test dependencies from how the project declared them at that commit (`candidate.
  _fallback_inputs`): dependency groups, else optional-dependency extras of the same name,
  else a test requirements file, always capped at the fix date. Poetry-era projects (rich
  before its move to `[project]`) cannot be resolved this way and stay Criterion 2 exclusions.
- **Dev configs list the dev bugs explicitly**, and a run refuses any manifest outside its
  config's `split` (`RunConfig.split`, default `dev`). Before this, the dev configs selected
  every manifest and would have run the model on test bugs once they existed.

## 2026-11-11: Screening batch 2 fixes

- **Optional test tooling the sandbox forbids is left out of the environment.** Starlette's
  conftest activates `blockbuster` (it flags blocking calls in async code) for every test;
  `blockbuster` patches builtins through `forbiddenfruit`, which calls `gc.get_referents`. The
  sandbox blocks every gc walk (red-team round 3), so every starlette test errored at setup and
  real crashes were excluded as "not a crash". The gc block stays; instead the starlette profile
  drops `blockbuster` and `forbiddenfruit` from the lock (`deps.drop`), a set-up the project
  supports (its conftest skips the fixture when `blockbuster` is missing). The affected
  candidates were screened again; two pass. Older starlette commits import `blockbuster`
  unconditionally in their conftest: without it the suite does not load, and with it every test
  errors at the gc block. Those candidates (starlette-3271, starlette-3545) cannot be used; the
  screening log records them as `preflight_fail` with the import error.
- **Bug kind, refined.** `library_raise` now also needs the raised class to be what the `raise`
  statement creates: an error while building the exception (starlette-3783:
  `raise ValueError(f"... {cast.__name__}")` fails with `AttributeError`) is a
  `python_failure`. A bare `raise` or `raise exc` still counts as deliberate.

## 2026-11-11: pytest's unraisable-exception plugin is switched off

Found by the CI known-answer job after batch 2: with an empty patch, click-3739's oracle tests
crash and leave temporary files open (the bug itself). Python closes them later, at a random
moment, and pytest's unraisable-exception plugin turned the resulting `ResourceWarning` into a
failure of whatever test was running then (click treats warnings as errors). An unrelated
baseline test (`test_basic_defaults[42-type1-42-int]`) failed in the grader's run, where the
oracle tests run first, but never in preflight's stability runs, where they run last.

Every pytest run of the harness (preflight, the agent's workbench, the grader, the plain
re-check, the repro oracle) now passes `-p no:unraisableexception`, so a warning fired during
garbage collection cannot fail an unrelated test. A harness bug fix (spec §15 (a)): verdicts of
a correct patch never depended on it, but labels (Regression Failure) and the agent's feedback
could. All 12 real bugs were re-run through preflight with the new command.

## 2026-11-11: Test set complete (30 bugs)

Screening stopped by the stop rule at 30 test bugs, after 208 candidates in all (35 passed
preflight: 5 dev and 30 test; 170 excluded; 3 preflight failures), every decision in the
screening log. The last rounds also looked at fixes merged up to two years back
(`--since 2024-11-11`), once the most crash-like candidates of the last 12 months were used up;
criterion 5 prefers recent fixes but does not require them. Fix dates in the test set run from
2025-08 to 2026-11. Kinds: 18 `python_failure`, 12 `library_raise` (after the kind rule
correction below). Per library: marshmallow,
more-itertools and tomlkit 6 each (the cap), starlette 4, click 3, werkzeug 2, requests,
flask and attrs 1 each.

## 2026-11-11: Subtest counting differs between pytest versions

Found by the per-bug known-answer job: with an empty patch, more-itertools-1182 was scored
`tampered`. Its oracle test has 4 failed subtests; pytest 9.1 (pinned by that bug) counts the
test itself as passed in the console summary, pytest 9.2 as failed, and the three-way check
assumed 9.2. The check now accepts either count for exactly those tests (`summary_mismatch`):
the console may show up to that many more "passed" and as many fewer "failed", nothing else.
The leeway only lets the console show more passes than the results file, never fewer, and the
per-test JUnit comparison is unchanged, so it cannot help a forged pass. A harness bug fix
(spec §15 (a)).

## 2026-11-11: Dress rehearsal (plan §7.3)

The final run's exact settings (`benchmark/configs/dry-run.yaml`: Sonnet 5.5, both pipelines,
3 trials, cache off, 400k cap) on the 5 dev bugs, run `dry-run-94b6a4a1-v2-claude-sonnet-5-5-
cli2.1.308`. Tuning only, never reported as results.

- 30 trials, 26 correct. Pipeline A: 5/5 bugs solved by the 2-of-3 rule (12/15 trials; repro
  oracle 13 valid, 1 invalid); Pipeline B: 5/5 (14/15). Tokens per run: A mean 144k (max 270k),
  B mean 64k (max 89k), all under the cap. 48 model calls.
- Checked on every trial folder: complete (`record.json`, `verdict.json`, `calls.jsonl`), a
  label on every failure, no oracle test id in any request, no cache hit, no prompt leak, no
  key, email or username in the logs.
- **Resuming:** a second run of the same command skipped all 30 finished trials. A run stopped
  hard in the middle of a trial (a separate small Haiku run) left only its call log; the next
  run set it aside (`trial_n.aborted-*`) and redid it. The same run then hit the usage guard (60
  calls in the hour), paused cleanly (exit code 3) and set the unstarted trial aside.
- **Fix:** a run killed while a sandbox container runs could leave that container behind. Sandbox
  containers now carry the label `org.tracerepro.sandbox`, and every real run first removes
  leftover labelled containers (never any other container) and stale agent workbenches.
- **For the final run:** about 290 model calls; with the guard at 60 calls per hour it takes
  roughly 5 to 6 hours, pausing and resuming on its own terms if the plan limit is reached.

## 2026-11-11: Bug kind rule, corrected before the freeze

The batch-2 refinement (a `raise` counts as deliberate only if the name it calls is the raised
class) misread libraries that build their error in a helper: marshmallow's
`raise self.make_error("required")` and tomlkit's `raise self.parse_error(...)` were labelled
`python_failure`. Only a class-like (capitalised) name is now compared with the raised class,
so `raise ValueError(f"... {x.__name__}")` failing with `AttributeError` is still a
`python_failure`, and helpers count as deliberate. Re-checked on every manifest: 5 change to
`library_raise` (marshmallow-2994, tomlkit-565, -581, -663, -701), regenerated by preflight
with nothing else changed.

## 2026-11-11: Analysis plan and freeze (M3)

`docs/ANALYSIS_PLAN.md` fixes the analysis before the final run; its statistics are
implemented and tested (`tracerepro.analysis.stats`). The contamination cutoff for Claude
Sonnet 5.5 is Anthropic's stated training data cutoff, June 2026 (models overview page, read
2026-11-11; the reliable knowledge cutoff is also June 2026). `benchmark/configs/final.yaml` is
the frozen run config. The `m3-freeze` tag marks the commit; after it, only harness bug fixes
(with an amendment when the spec changes).

## 2026-11-13: Final run (Phase 4) and the session-limit message

The final run (`final-935ffcd2-v2-claude-sonnet-5-5-cli2.1.308`) finished all 180 trials over
44 invocations of the runner (2026-11-12 to 2026-11-13), at the `m3-freeze` commit with a clean
tree. 88 trial attempts were set aside (`trial_n.aborted-*`) and rerun from scratch: 42 at a
usage-guard pause (22 hourly, 20 daily) and 46 at the plan's 5-hour "session limit". No
finished trial was rerun, so none of this touches a result.

The session-limit message ("You've hit your session limit · resets 7:20pm") did not match the
provider's limit pattern, so it was read as an ordinary rate limit: retried 4 times, then an
infrastructure failure. Fixed after the run (P5.1): the pattern now also covers "hit your …
limit", "session limit" and "weekly limit", which stop the run cleanly like the other plan
limits. Each rejected retry was also written to the usage guard's ledger; once, the 140 entries
from these rejections (28 trials × 5 tries, no tokens used) were removed by hand from
`work/llm-usage.json` so the daily cap counted only real calls (a backup was kept). The caps
themselves never changed.

## 2026-11-15: Pipeline A retired; Pipeline B2 to be measured on fresh bugs

The M4 study found no reliable difference between the pipelines (A 21/30, B 24/30 bugs; exact
McNemar p = 0.375) while A used about twice the tokens and failed mostly at its strict repro
gate (17 of 25 failures). The project owner decided to keep only Pipeline B and improve it.

- **Pipeline A is retired** from main: its runner, prompts and tests are removed, and configs
  that use it (`final.yaml`, `dry-run.yaml`, `measure-sonnet.yaml`, records of runs at the
  `m3-freeze` tag) are refused with a pointer to that tag. The study, its committed results and
  `make analyze` are unchanged. The repro gate stays: Pipeline B2 reuses it as a soft check.
- **Pipeline B2** (plan Phase 5b): a crash check that never blocks a patch, fix rules from the
  owner's second review, escalation on retry, harness-mediated file requests, and richer crash
  reports. Old B stays runnable from the same code (B2's switches off, prompts v2).
- **Fair measurement:** the 35 bugs already used become the practice set; B and B2 are compared
  on a fresh, frozen set of bugs that no model has seen, under a new analysis plan written
  before that run.

## 2026-11-16: Pipeline B2 settings, chosen on the practice set

Three tuning rounds (the fixed maximum), one trial per practice bug (35), Sonnet 5.5 at medium
effort, crash check and file requests on. None of these runs is a reported result.

| Round | Change | Bugs fixed | Tokens |
|---|---|---|---|
| 1 (`practice-b2-r1`) | retries at Sonnet high effort | 28/35 | 2.30M |
| 2 (`practice-b2-r2`) | retries with Opus 5.5 (first attempts reused from the cache) | 28/35 | 2.29M |
| 3 (`practice-b2-r3`) | round 1 plus the file-request fixes below | 24/35 | 2.67M |

- **Escalation: Sonnet at high effort on retries.** Only 4 of 35 trials retried; Opus wrote
  different patches but no outcome changed, at about twice the cost-equivalent for those calls.
  One model is also simpler to describe. Opus on retries is not used.
- **File requests: kept, with fixes.** In rounds 1 and 2 the model never asked for a file, even
  when it wrote that it could not see the code its fix relied on (tomlkit-701), and a reply
  with no fix was told "no more files can be shown" while its one request was unused (a harness
  bug, flask-5797). `request_v3` now says to ask rather than guess, and that feedback reminds
  the model of an unused request. In round 3 the request was used once (flask-5797: 3 files
  granted, still no fix).
- **Round 3's lower count is run-to-run variation, not the change.** None of the 6 bugs whose
  outcome flipped asked for files or mentioned them; 3 of the 5 that got worse (attrs-1529,
  marshmallow-2994, more-itertools-1118) already gave mixed results over old B's 3 trials in M4.
  One trial per bug cannot separate a difference of 4 bugs from noise, which is why the
  decision rests on the fresh set and 3 trials per bug.
- **Frozen for the fresh-set run (B2 arm):** Sonnet 5.5, effort medium, prompts v3 as in
  practice round 3, `crash_check: true`, `file_requests: true`, `retry_effort: high`, at most 2
  patch attempts. Old B arm: same harness, switches off, prompts v2. Both arms use schema 2
  crash reports.
- **Seen but not tuned for:** the fix rules do not reliably stop symptom fixes. In round 3
  marshmallow-2901 kept `required=True` but now skips the constant's default when it is set, a
  special case that fails the maintainers' `test_constant_with_required`. The fix-quality
  measures in the analysis plan are there to show this, not to hide it.

## 2026-11-16: The fresh test set (test2), 28 bugs, and the freeze of the second study

- **Screening.** The same frozen criteria, preflight and per-library cap of 6 as Phase 3, on
  candidates never screened before, marked `split: test2`. 231 candidates written in 4
  batches (51, 45, 85 and 50) and 221 screened (in batch 4, 10 belonged to libraries already
  at the cap): 28 passed, every decision in the screening log. Candidates within a batch
  were screened round-robin across libraries, in each library's crash-word order, so the stop
  rule could not favour the library that came first.
- **Fix dates.** Batches 1 and 2 used fixes from the last 12 months; that pool was used up
  (werkzeug and more-itertools had none left), so batches 3 and 4 looked back to 2024-11-16,
  as Phase 3 did. Criterion 5 prefers recent fixes but does not require them. 19 of the 28 bugs
  are from before the model's stated training cutoff (2026-06-30), so contamination is more
  likely than in M4 (16 of 30); it affects both arms alike.
- **Stopping.** After batch 3 (23 bugs), one more batch was set as the last before its results
  were seen: screening would end at 30 bugs or after batch 4, whichever came first. It ended
  after batch 4 with 28.
- **Per library:** jinja2 and more-itertools 6 each (the cap), starlette 5, marshmallow and
  werkzeug 4, tomlkit 2, click 1. The set leans toward fewer libraries than M4's.
- **Feature-like bugs kept.** starlette-3510 and starlette-3520 crash because the library
  rejects a keyword argument the fix adds: they pass every frozen criterion but read as
  missing features. Removing bugs by hand after seeing them is what the frozen criteria rule
  out, so they stay; the analysis plan defines them by that message and shows the primary
  numbers without them as well, descriptively.
- **The run.** Both arms in one harness version; the arms alternate bug by bug
  (`scripts/final-b2.sh`, `experiment --bug`) because two runs cannot share Docker at once:
  each clears the other's sandboxes when it starts.
- **Freeze.** `docs/ANALYSIS_PLAN_B2.md`, the configs `final-b2-arm-b.yaml` and
  `final-b2-arm-b2.yaml`, and the analysis code are committed and tagged `m5b-freeze` before
  any model sees a test2 bug. By the owner's decision (2026-11-16) steps 6 to 8 of Phase 5b go
  in one pull request, because the owner is away and merges would hold up the run; the tag
  marks the freeze point inside that pull request.

## Open (spec §16)

- Real deadline (the plan assumes 12 weeks from 2026-11-01).
- Where the write-up is published.
- (Settled: the final repository list is the 9 libraries above; jinja2, httpx, itsdangerous and
  markupsafe yielded no usable fix, rich and typer none that passed.)
