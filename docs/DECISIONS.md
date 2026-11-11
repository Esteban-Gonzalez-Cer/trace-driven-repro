# Decisions

Choices left open by the spec, or packaging details that do not change it. Spec changes go in
[`AMENDMENTS.md`](AMENDMENTS.md) instead.

## 2026-11-01: Python versions

- **Harness:** Python 3.12 (`.python-version`, `requires-python >=3.12`, CI on 3.12), managed
  by uv with a committed `uv.lock`.
- **Bug environments:** each bug pins one exact Python version in its manifest
  (`python_version`), 3.11 by default (`python:3.11-slim`, spec §5.2). The harness never runs
  bug code in its own interpreter.

## 2026-11-01: Package namespace

Spec §11 lists `src/dataset/`, `src/agent/`, and so on. These live under a single package,
`src/tracerepro/<module>/`, so they cannot clash with top-level names such as `dataset`. The
module layout is unchanged.

## 2026-11-01: Spec tag name

The frozen spec is tagged `spec-v1.0` rather than `v1.0`, so harness release tags (`v0.x`,
`v1.x`) can never collide with it.

## 2026-11-01: Repository visibility

Public from day one, at `github.com/Esteban-Gonzalez-Cer/trace-driven-repro`.

## 2026-11-10: Context cap in characters, imports ranked by use

- The context cap (plan §6.2) is 120,000 **characters**, not tokens, so a second model would get
  byte-identical context whatever its tokenizer. Token counts are logged per call.
- Imported files are ordered, and dropped over budget, by how often the trace files use the
  names they import from each file. An alphabetical order was tried first; on marshmallow-3082
  it dropped `fields.py`, the file the maintainers fixed, for no principled reason. Use counts
  come from the buggy code only and never look at the fix. Decided on the dev set, before any
  test-set bug is screened. Full rule in `METHODOLOGY.md`.

## Open (spec §16)

- Which two models, and the total API budget.
- Real deadline (the plan assumes 12 weeks from 2026-11-01).
- Where the write-up is published.
- Final repository list after early screening.
