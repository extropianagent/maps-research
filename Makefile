.DEFAULT_GOAL := help
.PHONY: help setup lint fmt test eval check

help: ## Show this list of targets
	@echo "Targets:"
	@echo "  setup   Install dependencies (uv sync)"
	@echo "  lint    Run ruff lint checks on maps, tests, evals"
	@echo "  fmt     Auto-fix lint issues and format maps, tests, evals"
	@echo "  test    Run the pytest suite"
	@echo "  eval    Run the simulated eval (maps eval sim)"
	@echo "  check   Run lint and test"

setup:
	uv sync

lint:
	uv run ruff check maps tests evals

fmt:
	uv run ruff check --fix maps tests evals
	uv run ruff format maps tests evals

test:
	uv run pytest -q

eval:
	uv run maps eval sim

check: lint test
