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
