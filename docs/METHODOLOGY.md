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
<<<<<<< SEARCH
exact lines from the current file
=======
replacement lines
>>>>>>> REPLACE
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
