# Analysis plan

Spec §9 written out for this study, committed and tagged (`m3-freeze`) **before the final run**.
Nothing here changes after the tag except through a logged amendment (spec §15). All analyses
are exploratory: effect sizes and intervals are reported, and no claim of significance is made
across multiple comparisons. With 30 bugs only large differences are detectable; the write-up
says so.

The statistics are implemented and tested before any result exists (`tracerepro.analysis.stats`,
`tests/analysis/test_stats.py`); `tracerepro.analysis.summary` collapses the trials.

## What is run

- **Model:** Claude Sonnet 5.5 (`claude-sonnet-5-5`), through the Claude CLI 2.1.308 (the
  project owner's decision: one model; DECISIONS.md, 2026-11-11).
- **Pipelines:** A (repro first) and B (direct patch), equal per-run token cap (400,000).
- **Bugs:** the 30 frozen test bugs (table below); 3 trials per bug and pipeline: 180 runs.
- **Config:** `benchmark/configs/final.yaml`, identical to the dress rehearsal
  (`dry-run.yaml`) except its name, bugs and split. Prompts v2, harness at the `m3-freeze` tag.
- **Reruns:** only trials that did not finish (infrastructure failures, usage pauses: set aside
  as `trial_n.aborted-*`). A finished trial is never rerun because of its result.

## Unit of analysis

The **bug**, not the trial. Trials are collapsed first: a bug is **solved** by a pipeline if at
least 2 of its 3 trials are graded correct (`stats.collapse`). A trial is correct when the patch
oracle says `correct` (spec §7.4); `tampered` counts as a failure and is reported separately.

## Primary comparison (H1)

Pipeline A versus B on patch accuracy (bugs solved, 2 of 3):

- **Exact McNemar test** on the paired per-bug outcomes: `b` = bugs solved by A only, `c` = by
  B only, two-sided p = 2 P(X <= min(b, c)), X ~ Binomial(b + c, 1/2) (`stats.mcnemar_exact`).
- **Paired difference** in per-bug success rate (fraction of a bug's 3 trials correct, A minus B,
  averaged over bugs) with a 95% percentile **bootstrap** interval: bugs resampled with
  replacement, 10,000 resamples, seed 20261111 (`stats.bootstrap_paired_difference`).
- Accuracy of each pipeline with a 95% **Clopper-Pearson** interval (`stats.clopper_pearson`).

## Secondary and descriptive measures

All proportions with raw counts and 95% Clopper-Pearson intervals:

- **Repro rate** (Pipeline A): runs whose repro test passes the repro oracle (spec §7.4), and
  bugs with a valid repro in at least 2 of 3 trials.
- **pass@3** (any trial correct) and **consistency** (all 3 correct), per pipeline.
- **Cost per arm:** tokens per run (mean, median, max), model calls, the CLI's list-price
  estimate (no money is spent on the plan), wall-clock time; diff size of correct patches.
- **Failure taxonomy (H2):** one label per failed run (spec §8, `evaluate.taxonomy`), counted per
  pipeline; categories 3 and 4 apply to A only, so the pipelines are compared on categories 1,
  2 and 5 to 8. State Deficit (category 6) is labelled by hand (below). H2 asks whether missing
  context (Fault Localization Miss, State Deficit) outnumbers syntax and tooling failures.
- **Bug kind:** results per `crash_kind` (18 `python_failure`, 12 `library_raise`), descriptive.
- **Contamination cohorts (H3):** results per cohort, descriptive only, no test. A bug is
  **pre-cutoff** if its fix date is on or before 2026-06-30 (Anthropic's stated training data
  cutoff for Claude Sonnet 5.5: June 2026, read on the models overview page on 2026-11-11),
  **post-cutoff** otherwise (`stats.cohort`): 16 and 14 bugs.

Not run: the secondary Pipeline A variant that falls back to direct patching (spec §6, "if
time allows"), and a second model (spec §2 target tier; DECISIONS.md).

## Manual audit (spec §7.5)

- **Every patch graded correct** is read, with its review report (`review.md`: scanner and
  behaviour flags), to catch tricks such as special-casing tests.
- A random **25% sample of patches graded incorrect** (seed 20261111, drawn by bug, pipeline and
  trial) is read to estimate the oracle's false-negative rate; the rate and its interval are
  reported.
- **State Deficit** labels are assigned by hand, blind to pipeline where possible, with a second
  reviewer on a sample.
- Findings go to `docs/AUDIT.md`.

## The test set

30 bugs from 9 libraries; fix dates 2025-08 to 2026-11. Kinds: 18 `python_failure`, 12
`library_raise`. Cohorts: 16 pre-cutoff, 14 post-cutoff.

| bug | fix date | cohort | kind |
|---|---|---|---|
| attrs-1529 | 2026-03-14 | pre-cutoff | library_raise |
| click-3493 | 2026-05-21 | pre-cutoff | python_failure |
| click-3739 | 2026-08-14 | post-cutoff | python_failure |
| click-3781 | 2026-08-20 | post-cutoff | python_failure |
| flask-5797 | 2025-08-18 | pre-cutoff | python_failure |
| marshmallow-2874 | 2025-12-05 | pre-cutoff | library_raise |
| marshmallow-2892 | 2026-01-23 | pre-cutoff | library_raise |
| marshmallow-2901 | 2026-03-25 | pre-cutoff | library_raise |
| marshmallow-2903 | 2026-03-25 | pre-cutoff | library_raise |
| marshmallow-2928 | 2026-04-01 | pre-cutoff | library_raise |
| marshmallow-2994 | 2026-07-22 | post-cutoff | library_raise |
| more-itertools-1088 | 2025-11-04 | pre-cutoff | python_failure |
| more-itertools-1118 | 2026-01-01 | pre-cutoff | library_raise |
| more-itertools-1182 | 2026-06-17 | pre-cutoff | python_failure |
| more-itertools-1193 | 2026-06-30 | pre-cutoff | python_failure |
| more-itertools-1294 | 2026-09-25 | post-cutoff | python_failure |
| more-itertools-1305 | 2026-09-29 | post-cutoff | python_failure |
| requests-7308 | 2026-03-30 | pre-cutoff | python_failure |
| starlette-3139 | 2026-02-14 | pre-cutoff | python_failure |
| starlette-3317 | 2026-06-10 | pre-cutoff | python_failure |
| starlette-3619 | 2026-10-23 | post-cutoff | python_failure |
| starlette-3783 | 2026-11-06 | post-cutoff | python_failure |
| tomlkit-565 | 2026-10-12 | post-cutoff | library_raise |
| tomlkit-581 | 2026-10-12 | post-cutoff | library_raise |
| tomlkit-610 | 2026-10-07 | post-cutoff | python_failure |
| tomlkit-631 | 2026-11-07 | post-cutoff | python_failure |
| tomlkit-663 | 2026-11-05 | post-cutoff | library_raise |
| tomlkit-701 | 2026-11-05 | post-cutoff | library_raise |
| werkzeug-3078 | 2026-01-02 | pre-cutoff | python_failure |
| werkzeug-3238 | 2026-08-13 | post-cutoff | python_failure |
