# trace-driven-repro

A small, reproducible benchmark that asks one question:

> Given only a runtime stack trace and static source context, how often can an LLM agent
> (a) write a pytest test that faithfully reproduces a crash and (b) produce a patch that the
> maintainers' own regression test accepts?

The pipeline takes a synthetic Sentry-style crash payload from a real, historical bug in a
pure-Python library, asks an LLM for a reproducing test and a minimal patch, and scores both in
a fresh, offline container against objective oracles. Two pipelines are compared at equal
compute: **A** (repro-first) and **B** (direct patch).

This is not a novel technology. The contribution is a careful, small-N study with a dual oracle
that checks repro fidelity, a tested containment layer, and honest statistics.

## Synthetic payloads

The crash payloads in `benchmark/payloads/` are **synthetic**. Each is built by running the
maintainers' regression test on the buggy commit inside the sandbox and formatting the resulting
traceback, exception message, source context and truncated local variables in a Sentry-like JSON
shape. They are not real production crash reports. Test-code frames are dropped so the held-out
oracle test never reaches the agent, and issue text, commit messages and PR descriptions are
never included.

## Results

Claude Sonnet 5.5, 30 real bugs from 9 Python libraries, 3 trials per bug and pipeline
(details and caveats in [`docs/RESULTS.md`](docs/RESULTS.md)):

| | Pipeline A (repro first) | Pipeline B (direct patch) |
|---|---|---|
| Bugs solved (2 of 3 trials) | 21/30 (70%; 95% CI 51% to 85%) | 24/30 (80%; 95% CI 61% to 92%) |
| Valid reproduction test (per run) | 71/90 (79%) | n/a |
| Tokens per run (mean) | 128k | 66k |

- **No reliable difference between the pipelines** (exact McNemar p = 0.375), and patching
  directly costs about half as much.
- **Most failures come from missing information, not broken code.** In Pipeline B, 15 of 22
  failures lacked the code to change or a fact the crash report did not contain.
- **The audit found no cheating** among the 133 fixes graded correct. The grader probably
  rejects some valid fixes: 1 or 2 in a sample of 12.

**Second study (Phase 5b), on 28 fresh bugs**: the improved Pipeline B2 (crash check, file
requests, fix rules) against B ([`docs/RESULTS_B2.md`](docs/RESULTS_B2.md)):

| | Pipeline B | Pipeline B2 |
|---|---|---|
| Bugs solved (2 of 3 trials) | 17/28 (61%; 95% CI 41% to 78%) | 18/28 (64%; 95% CI 44% to 81%) |
| Bugs with any correct trial | 17/28 | 21/28 |
| Tokens per run (mean) | 82k | 103k |

- **No reliable difference** (McNemar p = 1.0; the bugs split 1 to 0). B2's file requests cut
  the "code never shown" failures from 8 trials to 3.
- **Fix quality did not change**: about 1 in 6 correct trials in each arm fixes the symptom
  rather than the cause, and most correct patches fail the maintainers' wider tests.

Rebuild every table and figure from the committed results (no Docker, no keys):

```bash
make analyze
```

## Status

Phase 5 complete (milestone M4, results final): the final run (180 trials), the analysis from
logs, and the manual audit ([`docs/AUDIT.md`](docs/AUDIT.md)). Phase 5b complete (milestone
M5b): Pipeline A retired, Pipeline B2 built and measured against B on 28 fresh bugs
([`docs/RESULTS_B2.md`](docs/RESULTS_B2.md), audit [`docs/AUDIT_B2.md`](docs/AUDIT_B2.md); the
owner's second review of a sample is pending). Two further B2 switches were tried on three bugs
and not adopted ([`docs/DECISIONS.md`](docs/DECISIONS.md), Phase 5c). Next: the demo, with B2
as measured, and the write-up (Phase 6).

- Specification: [`docs/spec/spec-v1.0.pdf`](docs/spec/spec-v1.0.pdf) (frozen, tag `spec-v1.0`)
- Development plan: [`docs/spec/development-plan.pdf`](docs/spec/development-plan.pdf)
- Analysis plan: [`docs/ANALYSIS_PLAN.md`](docs/ANALYSIS_PLAN.md) (frozen, tag `m3-freeze`); second
  study: [`docs/ANALYSIS_PLAN_B2.md`](docs/ANALYSIS_PLAN_B2.md) (frozen, tag `m5b-freeze`)

## License

MIT. Dataset manifests reference upstream commits; no upstream code is copied.
