# Analysis plan: Pipeline B versus Pipeline B2 (Phase 5b)

Written, committed and tagged (`m5b-freeze`) **before the final run of the second study**, in
the same way as the first study's plan ([`ANALYSIS_PLAN.md`](ANALYSIS_PLAN.md), `m3-freeze`).
Nothing here changes after the tag except through a logged amendment. All analyses are
exploratory: effect sizes and intervals are reported, and no claim of significance is made
across multiple comparisons. With about 30 bugs only large differences are detectable; the
write-up says so.

The first study (M4: A 21/30, B 24/30) stays as published. This is a new, separate experiment
on a new set of bugs that no model has seen.

## What is run

- **Model:** Claude Sonnet 5.5 (`claude-sonnet-5-5`), effort medium, through the Claude CLI
  (version recorded in the run id), the same plan, lockdown and usage guard as M4.
- **Two arms, one harness version** (the same commit, the tag `m5b-freeze`):
  - **B** (old B): `benchmark/configs/final-b2-arm-b.yaml`. Prompts v2; crash check, file
    requests and escalation off. The same pipeline that scored 24/30 in M4.
  - **B2**: `benchmark/configs/final-b2-arm-b2.yaml`. Prompts v3 (fix rules from the owner's
    second review); the model's own crash check (a soft signal, never a gate); one request for
    up to 3 more library files; retries at high effort. Settings fixed on the practice set
    (DECISIONS.md, 2026-11-16).
  - Shared: at most 2 patch attempts, per-run token cap 400,000, context budget 250,000
    characters, cache off (trials are independent), crash reports schema 2 (Amendment 6) for
    both arms.
- **Bugs:** the `test2` split (table below); 3 trials per bug and arm.
- **Order:** the two arms alternate bug by bug (`scripts/final-b2.sh`): for each bug, both arms'
  trials run back to back, the arm that goes first alternating from one bug to the next, so a
  change in the model service over the run's hours affects both arms alike.
- **Reruns:** only trials that did not finish (infrastructure failures, usage pauses: set aside
  as `trial_n.aborted-*`). A finished trial is never rerun because of its result.

## Unit of analysis

The **bug**. A bug is **solved** by an arm if at least 2 of its 3 trials are graded correct by
the patch oracle (`stats.collapse`); `tampered` counts as a failure and is reported separately.

## Primary comparison

B2 versus B on patch accuracy (bugs solved, 2 of 3):

- **Exact McNemar test** on the paired per-bug outcomes: b = bugs solved by B2 only, c = by B
  only; two-sided p (`stats.mcnemar_exact`).
- **Paired difference** in per-bug success rate, **B2 minus B**, with a 95% percentile
  bootstrap interval (10,000 resamples, seed 20261111; `stats.bootstrap_paired_difference`).
- Accuracy of each arm with a 95% **Clopper-Pearson** interval.

`make analyze` builds them into `results/b2/` from `benchmark/results/b2.jsonl`
(`analysis.export ... --arms B B2 --name b2`; `report --name b2 --pair B2 B --also-without starlette-3510 starlette-3520`).

## Secondary measures

All proportions with raw counts and 95% Clopper-Pearson intervals:

- **pass@3** and **consistency** per arm.
- **Cost per arm:** tokens per run (mean, median, max), model calls, the CLI's list-price
  estimate, agent time per run, size of correct patches.
- **B2's own measures** (per trial): crash check sent, usable (it reproduced the crash on the
  buggy code), patches sent back because the check still failed and how many of those trials
  ended solved, submitted patches that pass their check but are graded incorrect, file
  requests (trials, files requested and granted), escalated retries and how many ended solved.
- **Failure labels** (spec §8), one per failed trial, per arm; `state_deficit` by hand.
- **By bug kind** and **by contamination cohort** (fix date on or before 2026-06-30, the stated
  training cutoff of Sonnet 5.5, versus after): descriptive, no test.

## Fix quality (what the grader cannot see)

The owner's second review of M4 found patches that pass the crash test while fixing only the
symptom. Two measures, both descriptive:

1. **Beyond-the-crash tests (automatic).** The maintainers' other new tests from the fix commit,
   left out of the oracle because they check values rather than a crash (Amendment 1), are run
   on every correct patch (`evaluate.extra_tests`), only for the tests that pass with the gold
   patch. Reported: correct trials per arm that also pass them, on the bugs that have such
   tests. Results in `benchmark/results/b2-extra.json`.
2. **Fix level (manual audit).** Every patch graded correct, in both arms, gets a `fix_level`
   from the owner's four questions (does it remove the mechanism; does it keep unrelated
   behaviour and what the caller asked for; does it hold beyond the failing input; is it at the
   right boundary):
   - `root_cause`: it removes the mechanism, as the maintainers' fix does;
   - `other_layer`: a legitimate fix at another boundary;
   - `symptom`: it lets the bug happen and works around its effect, or narrows or overrides
     what the caller asked for.
   Counted per arm, by trial. A `symptom` patch is not a `not_a_fix`: the headline grade stays
   the oracle's.

## Manual audit (spec §7.5)

Files in `benchmark/audit/b2/`, read by the report as in M4:

- **Every patch graded correct** (both arms): `ok`, `ok_with_note` or `not_a_fix`, plus
  `fix_level`. Audit-adjusted accuracy (`not_a_fix` counted as failure) is reported next to the
  headline.
- A random **25% sample of patches graded incorrect** (seed 20261111) to estimate the grader's
  false-negative rate.
- **State Deficit**, one decision per bug.
- **Second reviewer:** the project owner, on the same random sample sizes as M4 (10 correct
  patches, 5 incorrect, 5 State Deficit), drawn with seed 20261111 once the first pass is
  committed.
- Findings go to `docs/AUDIT_B2.md`; results to `docs/RESULTS_B2.md`.

## Not part of the result

The three practice rounds (`practice-b2-r1` to `-r3`) on the 35 already-seen bugs chose B2's
settings and are never reported as results.

## Feature-like bugs (descriptive)

Two bugs pass every frozen criterion but read as missing features: the crash is the library
rejecting a keyword argument that the fix adds (`unexpected keyword argument`):
starlette-3510 and starlette-3520. Defined by that message, before any model saw the set. They
stay in every result; the primary numbers are also shown without them, descriptively.

## The test set (`test2`)

28 bugs from 7 libraries, screened in 4 batches (DECISIONS.md, 2026-11-16); fix dates
2024-12 to 2026-11. Kinds: 17 `python_failure`, 11 `library_raise`. Cohorts: 19 pre-cutoff, 9
post-cutoff. 10 bugs have maintainers' tests outside the oracle (the last column counts them;
the beyond-the-crash measure uses those that pass with the gold patch).

| bug | fix date | cohort | kind | beyond-the-crash tests |
|---|---|---|---|---|
| click-3482 | 2026-05-22 | pre-cutoff | python_failure |  |
| jinja2-1702 | 2024-12-19 | pre-cutoff | python_failure |  |
| jinja2-1762 | 2024-12-20 | pre-cutoff | library_raise |  |
| jinja2-1782 | 2024-12-19 | pre-cutoff | python_failure |  |
| jinja2-1852 | 2024-12-20 | pre-cutoff | python_failure |  |
| jinja2-1984 | 2024-12-19 | pre-cutoff | python_failure |  |
| jinja2-2026 | 2024-12-19 | pre-cutoff | library_raise | 11 |
| marshmallow-2730 | 2025-01-06 | pre-cutoff | library_raise |  |
| marshmallow-2800 | 2025-01-23 | pre-cutoff | library_raise |  |
| marshmallow-2861 | 2025-10-31 | pre-cutoff | python_failure |  |
| marshmallow-2894 | 2026-02-04 | pre-cutoff | library_raise |  |
| more-itertools-1028 | 2025-07-14 | pre-cutoff | library_raise |  |
| more-itertools-1082 | 2025-10-10 | pre-cutoff | python_failure |  |
| more-itertools-1125 | 2026-02-02 | pre-cutoff | library_raise | 1 |
| more-itertools-1153 | 2026-04-13 | pre-cutoff | library_raise |  |
| more-itertools-1260 | 2026-09-18 | post-cutoff | python_failure | 1 |
| more-itertools-1276 | 2026-09-17 | post-cutoff | python_failure | 1 |
| starlette-3130 | 2026-08-08 | post-cutoff | python_failure | 2 |
| starlette-3372 | 2026-09-06 | post-cutoff | python_failure | 4 |
| starlette-3510 | 2026-09-05 | post-cutoff | python_failure |  |
| starlette-3520 | 2026-09-06 | post-cutoff | python_failure | 2 |
| starlette-3622 | 2026-10-17 | post-cutoff | python_failure | 2 |
| tomlkit-677 | 2026-11-06 | post-cutoff | python_failure | 6 |
| tomlkit-698 | 2026-11-05 | post-cutoff | python_failure | 3 |
| werkzeug-3071 | 2025-11-27 | pre-cutoff | python_failure |  |
| werkzeug-3110 | 2026-02-13 | pre-cutoff | library_raise |  |
| werkzeug-3136 | 2026-03-24 | pre-cutoff | library_raise |  |
| werkzeug-3148 | 2026-04-02 | pre-cutoff | library_raise |  |
