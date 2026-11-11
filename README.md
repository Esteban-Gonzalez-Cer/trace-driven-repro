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

## Status

Phase 2 (agent stage) complete: Pipelines A and B run end to end on the 5 dev bugs from one
command, with per-run usage measured. No results yet: dev-set runs only tune the harness.

- Specification: [`docs/spec/spec-v1.0.pdf`](docs/spec/spec-v1.0.pdf) (frozen, tag `spec-v1.0`)
- Development plan: [`docs/spec/development-plan.pdf`](docs/spec/development-plan.pdf)

## License

MIT. Dataset manifests reference upstream commits; no upstream code is copied.
