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

## Open (spec §16)

- Which two models, and the total API budget.
- Real deadline (the plan assumes 12 weeks from 2026-11-01).
- Where the write-up is published.
- Final repository list after early screening.
