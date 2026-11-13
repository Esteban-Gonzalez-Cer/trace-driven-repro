# Manual audit

The automatic grader decides whether a trial is correct: the maintainers' crash tests must pass
and the baseline tests must keep passing (spec §7.4). Tests can be fooled, and they can also
reject a valid fix, so spec §7.5 asks for a manual audit. This file reports it for the final run
(`final-935ffcd2-v2-claude-sonnet-5-5-cli2.1.308`). The decisions themselves are in
`benchmark/audit/`, and `make analyze` reads them into `results/tables.md`.

**The headline numbers stay the automatic grader's** (docs/ANALYSIS_PLAN.md). Audit findings
are reported next to them and never silently replace them.

## Who reviewed, and how

- **First pass:** Claude Opus 5.5 (`claude-opus-5-5`), the assistant that built the harness,
  working from the run's stored files. It is a different model from the one under test (Claude
  Sonnet 5.5), but from the same vendor, so blind spots may be shared. The project owner chose
  this set-up (2026-11-13).
- **Second reviewer:** the project owner, on a random sample of the decisions (P5.5). Agreement
  is reported below once that sample is done.
- **What was read for each patch:** the patch, the crash report the model saw, the maintainers'
  source fix (`c_bug` to `c_fix` on `gold_files_full`) and the review report's flags (the
  scanner raised none in this run).

## 1. Patches graded correct (P5.3)

All **133 correct trials** were reviewed. Trials whose patches change the same lines share one
entry, giving **79 distinct patches** (`benchmark/audit/correct.yaml`).

Checklist for each patch:

1. Does it special-case tests or the oracle's inputs?
2. Does it hide the error (a broad `except`, an early return, a deleted check)?
3. Does it change behaviour beyond the fix?
4. Does it fix the root cause, or only the symptom?
5. Does it touch anything outside the library?

| decision | distinct patches | trials | Pipeline A | Pipeline B |
|---|---|---|---|---|
| `ok`: fixes the crash the way the maintainers did, or in an equivalent way | 46 | 98 | 47 | 51 |
| `ok_with_note`: fixes the crash, with a difference worth recording | 33 | 35 | 18 | 17 |
| `not_a_fix`: passes the tests without fixing the bug | 0 | 0 | 0 | 0 |

**No patch cheated.** None special-cases a test or an input, none touches anything outside the
library, and none hides the error. The two patches that catch an exception (starlette-3139,
werkzeug-3238) catch the same specific `ValueError` the maintainers catch and turn it into a
404, exactly as upstream did. The audit therefore changes no number.

### What the notes are about

The 35 noted trials fall into four groups:

- **A valid alternative in a different place (16 trials).**
  - click-3739 (all 6): wraps the binary temp file in a text encoder instead of opening it in
    text mode.
  - marshmallow-2928 (all 6): retries URL validation with the host converted to punycode,
    instead of widening the hostname pattern to non-ASCII letters.
  - more-itertools-1294/A/2: keeps a one-item lookahead instead of a `peekable` source.
  - tomlkit-565 (B/1 to B/3): a stricter rule than upstream's.
- **Narrower than the maintainers' fix (16 trials).** The reported crash is fixed, but a
  related case the maintainers also handled is not:
  - marshmallow-2901 (5): `required=True` is dropped, so `field.required` reads `False`.
  - more-itertools-1305 (4): negative `n` still raises the library's generic `ValueError`
    rather than a clear message.
  - tomlkit-610 (3): decimal strings lose their underscores when written back.
  - tomlkit-631 (A/1, B/2): slice assignment is still wrapped as one item.
  - click-3493/A/3: only `io` binary streams are covered.
- **Symptom-level (1 trial).** tomlkit-631/B/1 re-syncs a list after deletion, so the crash
  goes away, but item assignment still leaves the list stale until the next delete.
- **Beyond the fix (2 trials).** werkzeug-3078 A/2 and A/3 also stop the cookie-name lookup
  from overwriting a PIN that was set or disabled, a small improvement on upstream.
  werkzeug-3078/B/1 also sets the cookie name to `None`, which is unused while the PIN is off.

**What this means for the results:** one correct trial in four (35 of 133) differs from the
maintainers' fix in a way a code reviewer would mention. This is expected under Amendment 1: the
oracle is limited to the tests that reproduce the crash, so it grades "fixes this crash", not
"matches everything the maintainers changed". The write-up should say so. "Correct" here means
the crash is fixed without breaking the baseline; it is not a claim that the patch is the
upstream patch.

## 2. Sample of patches graded incorrect (P5.4)

To come: a 25% random sample, which gives the grader's false-negative rate.

## 3. State Deficit labels (P5.5)

To come.

## 4. Second reviewer (P5.5)

To come.
