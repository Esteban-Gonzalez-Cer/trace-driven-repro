.PHONY: help setup lint fmt typecheck test test-docker build-images validate-gold run-benchmark analyze

help:
	@echo "setup         install dev environment and pre-commit hooks"
	@echo "lint          ruff check + format check"
	@echo "fmt           ruff fix + format"
	@echo "typecheck     mypy (strict) on src/"
	@echo "test          full pytest suite (docker tests skip without a daemon)"
	@echo "build-images  build one pinned image per bug manifest      (Phase 1)"
	@echo "validate-gold run gold-patch preflight on every manifest  (Phase 1)"
	@echo "run-benchmark run Pipelines A and B                       (Phase 2)"
	@echo "analyze       regenerate tables and figures from logs     (Phase 5)"

setup:
	uv sync
	uv run pre-commit install

lint:
	uv run ruff check .
	uv run ruff format --check .

fmt:
	uv run ruff check --fix .
	uv run ruff format .

typecheck:
	uv run mypy

test:
	uv run pytest

test-docker:
	uv run pytest -m docker

# Placeholders: exit non-zero so a stub is never mistaken for success.
build-images:
	@echo "build-images: not implemented yet (Phase 1)"; exit 2

validate-gold:
	@echo "validate-gold: not implemented yet (Phase 1)"; exit 2

run-benchmark:
	@echo "run-benchmark: not implemented yet (Phase 2)"; exit 2

analyze:
	@echo "analyze: not implemented yet (Phase 5)"; exit 2
