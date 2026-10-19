SHELL := /bin/bash
.DEFAULT_GOAL := help

PYTHON ?= python3.12
VENV ?= .venv
BIN := $(VENV)/bin
# Lock files never pick a release younger than this (supply-chain cool-down).
LOCK_COOLDOWN_DAYS ?= 14

.PHONY: help install lock env dev test lint security migrate check

help: ## List targets
	@grep -E '^[a-z-]+:.*## ' $(MAKEFILE_LIST) | awk -F':.*## ' '{printf "  %-10s %s\n", $$1, $$2}'

install: ## Create the venv, install hashed dev dependencies and the git hook
	$(PYTHON) -m venv $(VENV)
	$(BIN)/pip install --quiet --require-hashes --no-deps -r requirements/dev.txt
	$(BIN)/pre-commit install

lock: ## Regenerate hashed lock files (needs uv)
	@cutoff=$$($(PYTHON) -c "import datetime as d; print(d.date.today() - d.timedelta(days=$(LOCK_COOLDOWN_DAYS)))"); \
	for extra in runtime dev; do \
		flag=$$([ $$extra = dev ] && echo "--extra dev"); \
		uv pip compile pyproject.toml $$flag --universal --python-version 3.12 \
			--generate-hashes --exclude-newer "$$cutoff" --quiet -o requirements/$$extra.txt; \
	done

env: ## Create .env with fresh random secrets (never overwrites)
	$(BIN)/python tools/make_env.py

dev: ## docker compose up (web, prober, engine, postgres, redis) with hot reload
	@test -f .env || { echo "No .env — run 'make env' first."; exit 1; }
	docker compose up --build

test: ## pytest (no network) + coverage gate for sentinel.security
	$(BIN)/pytest --cov --cov-report=term-missing
	$(BIN)/coverage report --include='src/sentinel/security/*' --fail-under=85

lint: ## ruff, ruff format --check, mypy --strict
	$(BIN)/ruff check .
	$(BIN)/ruff format --check .
	$(BIN)/mypy

security: ## bandit, pip-audit, gitleaks
	$(BIN)/bandit -q -r src/sentinel
	$(BIN)/pip-audit --progress-spinner off --require-hashes --disable-pip -r requirements/dev.txt
	gitleaks git --redact --no-banner .

migrate: ## alembic upgrade head
	@echo "No migrations yet; Alembic arrives in Phase 1."

check: lint security test ## Everything CI runs; must pass before every commit
