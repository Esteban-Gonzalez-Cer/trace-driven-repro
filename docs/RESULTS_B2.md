# Results: Pipeline B versus Pipeline B2 (second study)

Final run of Phase 5b, on 28 fresh bugs (the `test2` split) that no model had seen before the
freeze. Claude Sonnet 5.5 (effort medium) through the Claude CLI 2.1.308. Two arms from one
harness version, the tag `m5b-freeze` (commit 3ea9444), 3 trials per bug and arm (168 runs):

- **B**, the first study's Pipeline B (prompts v2);
- **B2**, the improved pipeline:
  - prompts v3 with the owner's fix rules;
  - the model's own crash check (a soft signal);
  - one request for up to 3 more files;
  - retries at high effort.

Every number comes from `make analyze` (`results/b2/tables.md`, `results/b2/numbers.json`).
They follow the plan fixed before the run ([`ANALYSIS_PLAN_B2.md`](ANALYSIS_PLAN_B2.md)).
Analyses are exploratory, intervals are 95%, and with 28 bugs only large differences could be
detected. The first study ([`RESULTS.md`](RESULTS.md)) is unchanged.

## Headline

**B2 solved 18 of 28 bugs, B 17 of 28.** The bugs split 1 to 0: werkzeug-3110 for B2, none
for B. This is no evidence of a difference (exact McNemar p = 1.0). B2 cost about a quarter
more tokens per run and nearly twice the agent time.

What B2 changed is visible below the headline:

- **More bugs got at least one correct trial**: 21 against 17 (pass@3).
- **Fewer fault-localization misses**: 3 failed trials against 8. The file requests found the
  code B never saw.

The fix rules did not measurably improve fix quality.

## Primary comparison: patch accuracy

A bug counts as solved when at least 2 of its 3 trials are graded correct.

| | Pipeline B | Pipeline B2 |
|---|---|---|
| Bugs solved | 17/28 (61%; CI 41% to 78%) | 18/28 (64%; CI 44% to 81%) |
| Trials correct | 50/84 | 55/84 |

- Solved by both: 17; by neither: 10.
- Solved by B2 only: 1 (werkzeug-3110). By B only: none.
- Paired difference, B2 minus B: **+0.059** (bootstrap CI +0.000 to +0.131).
- Without the two feature-like bugs (starlette-3510, -3520; descriptive): B 16/26, B2 17/26.
  The audit found a third bug that reads as feature-like after the run (starlette-3130,
  unsolved by both). It is not left out, because that list was frozen.

## Secondary measures

| | Pipeline B | Pipeline B2 |
|---|---|---|
| pass@3 (any trial correct) | 17/28 (61%) | 21/28 (75%) |
| Consistency (all 3 correct) | 16/28 (57%) | 16/28 (57%) |
| Tokens per run: mean / median / max | 81,889 / 82,470 / 184,508 | 103,210 / 92,360 / 400,504 |
| Model calls per run | 1.18 | 1.35 |
| List-price estimate per run | $0.158 | $0.241 |
| Agent time per run, s: mean / median | 17.1 / 14.4 | 30.8 / 16.5 |

The runs used a Claude plan; the list price is the CLI's estimate at API prices.

## What B2's parts did (per trial, 84 B2 trials)

- **Crash check.**
  - Usable in 73 trials (it reproduced the crash on the buggy code). It did not reproduce in
    14 trials, all on 6 bugs.
  - It sent a patch back 4 times; 3 of those trials ended solved, all on click-3482. The audit
    found that retry also fixed only the symptom.
  - 17 submitted patches passed their own check and were still graded incorrect. A model's own
    check is a weak oracle.
- **File requests.**
  - Made in 18 trials: 30 files asked, 25 granted, refusals as designed.
  - They turned werkzeug-3110 into a solve: B failed all 3 trials because the code to change
    was never shown, and B2 solved 2 of 3 after asking for it. They also gave the first
    correct trials on jinja2-1702 and jinja2-1852 (1 of 3 each).
- **Escalation** (retry at effort high) in 11 trials, 6 ended solved.

## Why runs failed

| | Pipeline B | Pipeline B2 |
|---|---|---|
| syntax_or_import_error | 4 | 3 |
| fault_localization_miss | 8 | 3 |
| state_deficit (by hand) | 3 | 1 |
| regression_failure | 3 | 1 |
| incorrect_patch | 16 | 21 |
| **failed trials** | 34 | 29 |

B2 moved failures from "the code to change was never shown" to "the right code was shown, but
the patch was wrong". 7 bugs are unsolved by every trial of both arms:

- jinja2-2026, the one State Deficit bug;
- marshmallow-2730 and more-itertools-1260, where fixes were incomplete across sibling
  functions;
- starlette-3130 and starlette-3520, which are feature-like;
- tomlkit-677;
- werkzeug-3148, which turns on a policy choice.

## Fix quality (what the grader cannot see)

- **Beyond-the-crash tests**: the maintainers' other new tests, on the bugs that have them.
  Correct trials that also pass them: B 3/12, B2 3/13. Most correct patches fix the crash
  but not the wider behaviour the maintainers tested.
- **Fix level** (manual audit, by trial):

  | | Pipeline B | Pipeline B2 |
  |---|---|---|
  | root_cause | 38 | 42 |
  | other_layer | 3 | 5 |
  | symptom | 9 | 8 |

  The symptom fixes sit on three bugs in both arms alike: click-3482, starlette-3622 and
  marshmallow-2894. See [`AUDIT_B2.md`](AUDIT_B2.md).

## By bug kind and cohort (descriptive only)

| | Pipeline B | Pipeline B2 |
|---|---|---|
| library_raise (11) | 7 | 8 |
| python_failure (17) | 10 | 10 |
| pre-cutoff (19) | 13 | 14 |
| post-cutoff (9) | 4 | 4 |

Bugs fixed after the model's training cutoff were solved less often by both arms: 44% against
68–74%. One B2 patch on a 2024 bug is identical to the maintainers' fix, byte for byte.

## How far to trust the grader

- No correct patch was judged `not_a_fix`.
- 0 of 14 sampled rejections were real fixes (false-negative rate 0%, CI 0% to 23%).
- The owner's second review of a random sample is still to do; it is listed in `AUDIT_B2.md`.

## Run notes

- 2026-11-16, 10:29 to 18:00 UTC. The arms alternated bug by bug (`scripts/final-b2.sh`).
- 24 trials hit the plan's usage limit (18) or the hourly call guard (6). They were set aside
  and rerun, as the plan allows; no finished trial was rerun.
- One B2 run ended at 400,504 tokens: the cap is checked after each call, so a run can pass
  it by one call.
- Tokens: about 6.9M for B and 8.7M for B2.

## What this means

On fresh bugs, B2 is at least as good as B, and it costs more. Its file requests fix a real
gap: code the model was never shown. Its crash check rarely changed an outcome. Its fix rules
did not move fix quality. With 28 bugs, a gain of one bug is within noise. **B2 becomes the
demo pipeline for Phase 6**, as planned, for its file requests and its crash check (which can
become the PR's regression test). The demo will not claim better fixes than B.
