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

## 2026-11-10: Models and how they are reached (spec §16)

- **Main model:** Claude Sonnet 5.5 (`claude-sonnet-5-5`). Claude Haiku 5.5 (`claude-haiku-5-5`)
  is used only while testing the plumbing. Claude only for now; the client takes one provider
  per vendor, so a second vendor can be added later (spec §15 lists two models as frozen; until
  then the minimum tier, one model, applies).
- **Access through the Claude plan, not the paid API.** Every call goes through the `claude`
  CLI in script mode (`claude -p`), locked down to a plain text-in, text-out model (no tools,
  empty working folder, our own system prompt). No API key is used and no API money is spent.
  What is measured is therefore "the model as served by Claude Code in script mode, tools off";
  the CLI version is logged with every call (`LIMITATIONS.md`).
- **Settings recorded per run:** model id, effort and CLI version. There is no temperature:
  current Claude models accept only their default sampling settings, and the CLI does not
  expose it.
- **Output limit:** set through `CLAUDE_CODE_MAX_OUTPUT_TOKENS`. Checked with CLI 2.1.308: when
  an answer outgrows it, the CLI asks the model to continue up to four turns, then reports an
  error and drops the partial text. The harness counts that, and any reply that needed more
  than one turn, as output cut off (`max_tokens`): a failed attempt, never an infrastructure
  error. The real limit is therefore up to four times the setting; the default (16,000) is far
  above any patch the size limits allow.
- **Overhead:** the CLI adds about 590 prompt tokens of its own to every call (measured with
  `make llm-smoke`); they are counted in the logged usage and the per-run token cap.
- **Spending:** none on the API. Plan usage is limited by the usage guard (calls per hour and
  per day, a pause between calls) and by the per-run token cap.

## Open (spec §16)

- Real deadline (the plan assumes 12 weeks from 2026-11-01).
- Where the write-up is published.
- Final repository list after early screening.
- Second vendor, if any.
