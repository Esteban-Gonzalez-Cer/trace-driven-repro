# Changelog

One line per merged task (plan §2, definition of done).

## Unreleased

- Spec v1.0 and development plan committed; tagged `spec-v1.0`.
- Repo layout per spec §11; uv + ruff + mypy + pytest + pre-commit (gitleaks) tooling.
- GitHub Actions CI: lint/type/test, Docker sandbox smoke, secret scan.
- Makefile with `test` and placeholder `build-images`, `validate-gold`, `run-benchmark`, `analyze`.
- Toy fixture `tests/fixtures/tiny_lib` (buggy, fixed, gold patch, oracle and baseline tests).
- Frozen selection criteria and screening log started.
- Amendment 1: crash-only oracle and minimal gold_files for mixed fixes (#15).
- Amendment 2: exclude crashes whose traceback has no library frame; define target_frame (#16).
- Image builder: `make build-images` builds pinned `bug_env:{id}` images from manifests, with a freeze record (#10).
- Container runner: locked-down `run_sandboxed`/`run_pytest` with throwaway workspaces and a harness pytest plugin that records exception type and frames per failure (#11).
- Patch validator: host-side `validate`/`apply_patch` enforcing every spec §7.1 rule, with shared path classification (#12).
- Preflight automation: `make validate-gold` derives oracle, baseline, flaky set, minimal gold files and a synthetic payload per candidate; Amendment 3 (docs/changelog edits dropped from gold) (#13).
- Five dev bugs validated by automated preflight (flask-6096, marshmallow-3004/3016/3082, more-itertools-1342); 25 candidates screened (#13).
- Preflight removes the image of every candidate that does not pass (`--keep-images` to keep them).
- Patch oracle with known-answer tests for every validated bug and red-team tests (injection payload, malicious patches); CI known-answer job (#14).
- Amendment 4, result integrity: static patch scanner, runtime audit hook, canary test and three-way result check (new `tampered` verdict), behaviour flags against the gold run, and a review report for the manual audit.
- Red-team agent (`.claude/agents/sandbox-red-team.md`) and proof-based referee (`tracerepro.redteam.challenge`); round-1 findings fixed: plain-run check against grader-aware patches, hidden canary, timeouts blamed on the patch, in-memory size-capped `/repo` and `/out`, crash-proof scanner, exec-code gc bypass and `/proc` reads.
- Red-team round 2 fixes: sandbox supervisor as process 1 with a per-run secret (unforgeable exit codes, result files and audit events), leftover processes killed before results, cwd-relative and thread-aware audit hook, writers trusted by code identity, fail-closed parsing of container output, stronger plain re-check, case-insensitive validator paths and a 64 KB patch cap, new scanner rules.
- Red-team round 3 fixes: three plain re-checks for a correct verdict, gc walks always blocked and pytest `__code__` swaps blocked by code identity, test-file reads by code under test logged.
- LLM client: provider interface, Claude CLI provider through the Claude plan (locked down: no tools, empty folder, allow-listed environment, fail-closed replies), retries, call log, dev cache, per-run token cap and usage guard; `make llm-smoke` (#29).
- Prompts v1 and output contracts: versioned prompt files, data blocks with random per-message tags, strict repro and search-and-replace edit parsing, edits turned into a git diff for the validator, leak check (#31).
- Context assembler: trace files plus 1-hop imports ranked by use, tests excluded, 120k-character cap with a documented truncation rule (outline, drop, window, cut); deterministic (#30).
- Pipeline B and the benchmark runner: agent-time workbench on the buggy code and visible tests, feedback loop, best-patch rule, trial folders, resumable `make run-benchmark` with separate evaluation (#32).
- Context cap raised to 250,000 characters; prompts v2 (no more files will be shown, give the best fix); `declined` outcome; Amendment 5: Fault Localization Miss checked line by line, context coverage recorded per trial.
