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
