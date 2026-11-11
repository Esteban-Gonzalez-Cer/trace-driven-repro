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
- **Environments that cannot be pinned.** Candidates whose test dependencies cannot be pinned
  reproducibly at the buggy commit are excluded (criterion 2), for example projects still on
  Poetry's old format; the screening log records each case.
- **Screening order.** Candidates whose fix descriptions mention an exception or a crash are
  screened first (DECISIONS.md), so the test set may lean toward bugs described that way.
- **Test suites that need gc access.** The sandbox blocks every gc walk (red-team round 3), so
  bugs whose test suite cannot run without a tool that patches builtins through gc (older
  starlette commits and `blockbuster`) cannot be used.
- **One model.** Only Claude Sonnet 5.5 is evaluated (the spec's minimum tier), so nothing can be
  said about other models; see DECISIONS.md for why a second vendor was set aside.
- **Contamination is only described.** Cohorts split at the model's stated training cutoff
  (June 2026): 16 pre-cutoff and 14 post-cutoff bugs. Small groups, no test; a stated cutoff is
  approximate.
