.PHONY: help setup lint fmt typecheck test test-docker build-images validate-gold llm-smoke run-benchmark analyze

help:
	@echo "setup         install dev environment and pre-commit hooks"
	@echo "lint          ruff check + format check"
	@echo "fmt           ruff fix + format"
	@echo "typecheck     mypy (strict) on src/"
	@echo "test          full pytest suite (docker tests skip without a daemon)"
	@echo "build-images  build bug_env:{id} images (MANIFESTS=..., BUILD_FLAGS=--no-cache)"
	@echo "validate-gold run gold-patch preflight (CANDIDATES=...); writes manifests and payloads"
	@echo "llm-smoke     real Claude CLI checks: ping, hands-off, max-tokens (uses plan; MODEL=...)"
	@echo "run-benchmark run the agent pipelines and score them (CONFIG=..., uses plan + Docker)"
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
# MANIFESTS defaults to every benchmark/manifests/*.yaml; e.g.
#   make build-images MANIFESTS=tests/fixtures/tiny_lib/manifest.yaml
build-images:
	uv run python -m tracerepro.dataset.image_builder $(BUILD_FLAGS) $(MANIFESTS)

# CANDIDATES defaults to every benchmark/candidates/*.yaml.
validate-gold:
	uv run python -m tracerepro.dataset.preflight $(CANDIDATES)

# Uses a little of the Claude plan. Never run in CI.
llm-smoke:
	uv run python -m tracerepro.agent.smoke $(if $(MODEL),--model $(MODEL))

# Uses the Claude plan and Docker. CONFIG defaults to the dev-set run.
CONFIG ?= benchmark/configs/dev.yaml
run-benchmark:
	uv run python -m tracerepro.experiment $(CONFIG)

analyze:
	@echo "analyze: not implemented yet (Phase 5)"; exit 2
