# Methodology

TODO: Phase 3, frozen with the analysis plan at M3.

## Prompts and output contracts (plan §6.3)

**Prompts** are versioned files in `prompts/` (`system_v1.md`, `patch_v1.md`, `repro_v1.md`).
Every call logs the prompt's version and sha256. Prompts are frozen with a git tag before the
final run (spec §6).

**Message layout.** Our instructions come first, outside any block. The material follows in
data blocks, in a fixed order: `crash-report` (the payload), `source` (the context), then for
Pipeline A `repro-test` and `repro-result`, and on a retry `test-results`. Each block is
wrapped in a start tag and end tag with a random 8-hex-digit suffix drawn per message (for
example `<data-source-9f2c41aa>`), re-drawn until it appears nowhere in the material.

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

Every token of every call counts against the run's cap (provisional 300,000 until measured).
When the cap is reached the run stops and submits its best patch so far.

**What the agent never sees:** the fix commit, the oracle tests, or any evaluation result. The
workbench is built from the `c_bug` image, the agent package never imports the evaluator (a
test checks this), and a test checks that no oracle test id reaches the model.

**Evaluation** happens afterwards, separately: the submitted diff is scored by the patch oracle
in a fresh container (`verdict.json`), with the review report for the manual audit
(`review.md`).
