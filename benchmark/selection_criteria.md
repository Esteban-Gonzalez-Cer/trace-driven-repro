# Bug selection criteria (FROZEN)

Frozen with spec v1.0 (tag `spec-v1.0`, 2026-10-20) before any screening. Changes only through
a logged amendment in [`docs/AMENDMENTS.md`](../docs/AMENDMENTS.md) (spec §15).

## Candidate pool (spec §5.1)

Pure-Python libraries only: click, requests, flask, jinja2, attrs, marshmallow, rich, werkzeug,
starlette, httpx, typer, sqlparse. Backups if yield is low: tomlkit, itsdangerous, markupsafe,
more-itertools.

## Selection criteria (spec §5.2)

A bug is a candidate only if all of the following hold:

1. The fix commit has a regression test and the bug is a crash (an uncaught exception with a
   stack trace).
2. The parent commit builds in `python:3.11-slim` with pinned dependencies, and the test suite
   runs offline.
3. The gold fix touches only source files, with no deny-listed non-test files, in at most 3
   files and about 40 changed lines.
4. The failing behavior is deterministic across repeated runs.
5. Fix date is recorded. Prefer fixes from the last 12 months.

Each bug pins one exact Python version in its manifest (`python_version`); 3.11 is the default.
A bug that needs a different interpreter (for example, a 3.12-only code path) may pin it, and
the reason goes in the manifest's `notes`.

## Gold-patch preflight (spec §5.4, host checks, all must pass)

1. **Gold test:** the upstream regression test fails on the parent (buggy) commit with the
   target exception and passes on the fix commit.
2. **No gold regressions:** no test that passes on the buggy commit fails on the fix commit.
3. **Stability:** the baseline suite run 3 times on the buggy commit gives identical pass/fail
   results. Flaky tests are recorded and excluded from the baseline set.
4. **Offline isolation:** the image builds with network on, then runs pytest with `--net=none`.

Bugs failing any check are dataset exclusions, not model failures, and are never counted in a
denominator. Every decision is recorded in [`screening_log.jsonl`](screening_log.jsonl).

## Screening log fields

| Field | Meaning |
|---|---|
| `id` | `<repo>-<issue or PR number>` |
| `repo` | `owner/name` on GitHub |
| `issue_url`, `fix_pr_url` | Provenance (never shown to the agent) |
| `c_bug`, `c_fix` | Parent commit and fix commit (full hashes) |
| `fix_date` | Merge date of the fix (ISO 8601) |
| `exception` | Exception type raised by the bug |
| `src_files_changed`, `src_lines_changed` | Non-test files and lines (+/-) in the gold fix |
| `has_regression_test` | The fix adds or changes a test that exercises the crash |
| `decision` | `candidate`, `excluded`, `preflight_pass`, or `preflight_fail` |
| `reason` | Why, for every decision |
| `screened_on` | Date of the decision |
