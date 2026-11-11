# Methodology

TODO: Phase 3, frozen with the analysis plan at M3.

## Prompts and output contracts (plan §6.3)

**Prompts** are versioned files in `prompts/` (`system_v1.md`, `patch_v1.md`, `repro_v1.md`).
Every call logs the prompt's version and sha256. Prompts are frozen with a git tag before the
final run (spec §6).

**Message layout.** Our instructions come first, outside any block. The material follows in
data blocks, in a fixed order: `crash-report` (the payload), `source` (the context), then for
Pipeline A `repro-test` and `repro-result`, and on a retry `test-results`. Each block is
wrapped in a start tag and end tag with an 8-hex-digit suffix (for example
`<data-source-9f2c41aa>`): a sha256 hash of the whole message, re-drawn until it appears
nowhere in the material. The same message always gets the same tags, so runs are reproducible
and identical requests can be cached.

**Repro contract.** Exactly one fenced Python code block (tagged `python`, `py` or untagged;
other languages are ignored), holding the whole test file. The harness saves it in its own
place; it is never part of the patch.

**Patch contract.** One or more search-and-replace edit blocks:

```
FILE: path/to/file.py
exact lines from the current file
replacement lines
```

The harness applies them to a scratch copy (each SEARCH text must occur exactly once in the
file as it stands after earlier edits; an empty SEARCH creates a new file) and lets git write
the diff. That diff goes through the usual validator and is what the evaluator re-applies. The
model never writes a unified diff, since models often get its line numbers wrong.

**Failed attempts.** A reply that breaks a contract counts as one failed attempt, labelled per
spec §8:

| Problem | Label |
|---|---|
| no code block, several, unclosed; no edit block; malformed block; SEARCH not found or found more than once; edits that change nothing; a reply over 400,000 characters | Syntax or Import Error |
| a path that is absolute, contains `..`, a backslash or `.git`, or is a symlink | Scope Violation |
| a valid edit to a test, config, dependency or CI file, or over the size limits | Scope Violation (by the validator) |
=======
## Context given to the agent (spec §6, plan §6.2)

Built by `tracerepro.context.assembler` from the synthetic payload and the `c_bug` source tree.
The agent gets no browsing tools; this is everything it sees besides the payload.

**Selection.**
1. *Trace files:* the files of the payload's in-app frames, innermost frame first.
2. *1-hop imports:* source files the trace files import (absolute and relative imports, found
   with Python's `ast`), resolved inside the same top-level package. Standard library and
   dependencies are not included.
3. *Never included:* test files (`tests/`, `test_*.py`, `*_test.py`), non-Python files, and
   anything outside the package. The held-out oracle tests therefore cannot reach the agent.

**Order.** Trace files first. Imported files follow, ordered by how often the trace files use
the names imported from them (most used first; ties by path). The ranking only looks at the
buggy code, never at the fix.

**Format.** Each file starts with `=== file: <path> (<role>[, <shape> of <N> lines])`
and shows its lines with their real line numbers; skipped lines are shown as `...`.

**Cap and truncation rule.** The rendered context is capped at 250,000 characters (about 60,000
tokens; raised from 120,000, see `DECISIONS.md`). The cap is in characters so every model gets byte-identical context whatever its
tokenizer; real token counts are logged from each reply. Over the cap, these stages apply in
order, stopping as soon as the context fits:

1. *outline:* imported files are cut to their top-level definitions: imports, the first lines
   of assignments, class and function signatures with their docstrings, and methods'
   signatures.
2. *drop:* imported files are dropped, least used first; then any dropped file that still
   fits is added back as an outline, most used first.
3. *window:* trace files keep the lines within 40 lines of each frame (then 15), plus their
   outline, their imports and the signatures of the definitions enclosing each frame; if that
   is still too big, the outline goes. Dropped imported files are then added back as above.
4. *cut:* the last lines are cut at the cap.

The stage reached, the files dropped and the exact line ranges shown are logged per trial. On
the 5 dev bugs: marshmallow-3004, -3016 and -3082 fit whole (35k to 195k characters);
flask-6096 reaches *outline* (191k); more-itertools-1342 reaches *drop* (216k: its 5,654-line
trace file whole, its one import dropped). The lines the maintainers changed for the crash are
shown in all five.

**Did the context cover the fix?** (Amendment 5.) After the run, the crash part of the gold diff
is compared with the line ranges shown. The context covers the fix only if every line the fix
changes was shown (for an insertion, the line before it). Recorded for every trial in
`verdict.json` (`context_coverage`); a failed run whose context did not cover the fix is a
Fault Localization Miss.

## Pipeline B and the feedback loop (plan §6.4)

1. Ask for a patch with the crash report and the context.
2. Turn the reply into a diff (output contracts above), check it with the validator, and try it
   on the buggy code against the **visible** baseline tests: the manifest's baseline tests that
   live in test files present at `c_bug`. A regression is a baseline test that did not pass,
   or whose test file no longer collects.
3. If anything went wrong and an attempt remains, send back what happened inside a
   `test-results` data block, using the `feedback_v1` prompt: the failing test names (at most
   30) and the last 60 lines of the run; or why the reply could not be used; or why the
   validator rejected the patch. Then ask for a revised patch, applied to the original files.
   A readable reply with no edit block is recorded as `declined` (the model gave no fix), not
   as a malformed reply; the feedback reminds it that no more files can be shown.
4. At most 2 patch replies. The submitted patch is the last one that ran clean; if none did,
   the last one that applied; otherwise nothing is submitted (`no_patch`).

Every token of every call counts against the run's cap: 400,000 tokens, the same for both
pipelines (set from the measurement in `DECISIONS.md`).
When the cap is reached the run stops and submits its best patch so far.

**What the agent never sees:** the fix commit, the oracle tests, or any evaluation result. The
workbench is built from the `c_bug` image, the agent package never imports the evaluator (a
test checks this), and a test checks that no oracle test id reaches the model.

**Evaluation** happens afterwards, separately: the submitted diff is scored by the patch oracle
in a fresh container (`verdict.json`), with the review report for the manual audit
(`review.md`).

## Pipeline A: repro first (plan §6.5, spec §6)

1. **Ask for a repro test** (prompt `repro`): one pytest file reproducing the crash. The harness
   places it at `repro/test_repro.py` in the workspace. It is never part of the patch, so the
   validator's ban on test edits still holds for patches.
2. **Agent-time gate:** the test runs alone on the buggy code. It passes the gate only if a test
   in it fails with the crash report's exception type, raised from the crash report's innermost
   library frame (file and function, not the line). This is preflight's own rule
   (`crash_target`), and the target is read from the payload the agent was given, never from
   the manifest or the oracle. Outcomes: `valid`; `no_repro` (the test passes on the buggy code);
   `invalid_repro` (another exception or place, raised in the test itself, or the run did not
   finish); `broken` (the file does not import or holds no test: Syntax or Import Error);
   `declined` (no code block).
3. **Retry once** (prompt `repro_feedback`): what happened, with the end of the output.
4. **No valid repro after 2 replies:** the run stops (the strict rule) with status `no_repro` if
   the last test passed on the buggy code, otherwise `invalid_repro`. No patch is asked for.
5. **Patch stage**, in the same conversation: the patch prompt with the valid repro test and its
   failure output in data blocks. Each patch runs with the repro test and the visible baseline.
   It is clean only if the repro now passes and no baseline test regresses; otherwise the
   feedback says which (`repro_still_fails` or regressions). At most 2 patch replies; the same
   best-patch rule as Pipeline B.

Both pipelines share the per-run token cap; Pipeline A's repro replies count against it.

## Repro oracle (spec §7.4)

Pipeline A's saved repro test is judged after the run, in fresh containers, by
`tracerepro.evaluate.repro_oracle`. It is valid only if all three hold: it fails on `c_bug`; the
failure is the manifest's `target_exception` from its `target_frame` (preflight's
`crash_target` rule, file and function); and it passes unchanged on the fix (`c_bug` plus the gold
source diff). The test is model-written code: it runs with the audit hook, a hidden canary test
and the three-way result check; results that cannot be trusted make it `tampered`, never valid.
Verdicts: `valid`, `no_repro`, `invalid_repro`, `broken` (does not import or holds no test),
`tampered`, `infra_error`. The repro rate counts `valid` runs. A correct patch is counted as
solved whatever the repro verdict; the two rates are reported separately.

## Failure labels (spec §8)

Every failed run gets exactly one label (`tracerepro.evaluate.taxonomy`), the first that holds:

1. **Scope Violation:** the submitted patch was rejected by the validator or scanner, or, with no
   patch submitted, an attempt was.
2. **Syntax or Import Error:** no usable patch or test came out. This includes replies that broke
   the output format, were cut off or refused, declined to give a fix (`declined`), a patch that
   did not apply, and a repro test that does not import.
3. **No-Repro** (A only): the repro test passes on `c_bug`.
4. **Invalid Repro** (A only): wrong exception or frame, or fails on the fix.
5. **Fault Localization Miss:** the lines the fix changes were not shown (Amendment 5).
6. **State Deficit:** by hand, in Phase 5.
7. **Regression Failure:** the patch breaks a stable baseline test.
8. **Incorrect Patch:** applies, keeps the baseline, fails the oracle.

For Pipeline A, categories 3 and 4 come from the repro oracle when a test was produced, and from
the agent-time gate when the run stopped without a valid repro (the strict rule). Without a
patch, the labels of the stage that ended the run are compared by precedence. Two outcomes stay
outside the list and are reported separately: `tampered` (Amendment 4, counted as a failure) and
`infra_error` (no label; the trial is rerun). `verdict.json` holds `label`, `solved` and, for
Pipeline A, `repro_oracle`.

## Screening the test set (plan §7.2)

1. **Finding candidates** (`python -m tracerepro.dataset.find_candidates LIB ...`): merged pull
   requests of each pool library from the last 12 months, read through `gh`. A pull request is
   kept mechanically if its author is not a bot, it changes a test module and library source,
   it changes no deny-listed file, and its source change is at most 3 files and 60 lines (the
   exact criterion 3 limit is applied by preflight on the real diff). The file split is
   preflight's own (`split_changes`). Pull requests already screened are skipped.
2. **Order:** kept pull requests are listed with those whose title or description names an
   exception, a traceback or a crash first, newest first within each level. Order decides only
   which candidates are screened first; nothing is excluded from a title or description.
3. **Preflight in batches** of 10 to 20 (`make validate-gold CANDIDATES=...`): every screened
   candidate gets a screening-log row; a batch ends with a summary (decisions, exclusion
   reasons, kinds of the passing bugs, test bugs per library).
4. **Balance and stop rule:** at most 6 test bugs per library (later candidates of a full
   library are skipped and not logged); screening stops at 30 test bugs. No model is run on a
   test bug before the freeze.
