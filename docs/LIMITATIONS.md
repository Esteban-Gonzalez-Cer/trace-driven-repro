# Limitations

TODO: Phase 6. Must cover contamination, synthetic payloads, small N, oracle false negatives, Python-only scope, and Docker isolation limits.

## Recorded so far (to be expanded in Phase 6)

- **Synthetic payloads.** Crash reports are built from the maintainers' regression test, not taken
  from production. The traceback starts at the outermost library frame, because test frames are
  dropped to keep the oracle held out. A real report would also show application frames.
- **Crashes outside library code are out of scope** (Amendment 2). Bugs whose traceback never
  enters the library's own source are excluded at curation; this is a scope limit, not a model
  result.
- **Reduced oracles for mixed fixes** (Amendment 1). Where a fix bundles an unrelated change, only
  the crash part is graded. The write-up reports how many bugs this affects.
- **"Passes on the fix commit"** is checked as the buggy commit plus the gold source diff, which
  equals the fix commit except for test and documentation files (Amendment 3).
- **Results are written inside the sandbox** and could in principle be forged in-process by a
  malicious patch; see `SECURITY.md`.
- **Model access through Claude Code.** Calls go through the Claude CLI in script mode with tools
  off, not the bare API: the CLI adds its own small prompt overhead, temperature cannot be set,
  and its version is part of the setup (logged with every call). Results describe the model as
  served this way.
