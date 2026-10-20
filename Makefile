SHELL := /bin/bash
.DEFAULT_GOAL := help

PYTHON ?= python3.12
VENV ?= .venv
BIN := $(VENV)/bin
# Lock files never pick a release younger than this (supply-chain cool-down).
LOCK_COOLDOWN_DAYS ?= 14
# Throwaway services from compose.test.yml (CI provides its own service containers).
TEST_DATABASE_URL ?= postgresql+asyncpg://sentinel@127.0.0.1:55432/sentinel_test
TEST_REDIS_URL ?= redis://127.0.0.1:56379/0
export TEST_DATABASE_URL TEST_REDIS_URL

.PHONY: help install lock env dev node test-services test lint security migrate catalog catalog-schema check

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

dev: ## docker compose up (web, engine, postgres, redis, mailpit; prober once `make node` ran)
	@test -f .env || { echo "No .env — run 'make env' first."; exit 1; }
	@if grep -q '^PROBE_NODE_ID=.' .env; then docker compose --profile probe up --build; \
	else echo "No probe node yet: run 'make node' in another terminal to start the prober."; docker compose up --build; fi

node: ## Register a local probe node, store its credentials in .env, start the prober
	@grep -q '^PROBE_NODE_ID=.' .env && { echo "A node is already registered in .env."; exit 1; } || true
	docker compose run --rm -T migrate python -m sentinel.probe register local-dev "docker compose" >> .env
	docker compose --profile probe up -d prober

test-services: ## Start the throwaway test Postgres/Redis (skipped in CI)
	@if [ -z "$$CI" ]; then docker compose -f compose.test.yml up -d --wait --quiet-pull >/dev/null; fi

test: test-services ## pytest (no network) + coverage gates
	$(BIN)/pytest --cov --cov-report=term-missing
	@for pkg in security tenancy accounts sources graph catalog probe engine; do \
		echo "coverage gate: sentinel.$$pkg"; \
		$(BIN)/coverage report --include="src/sentinel/$$pkg/*" --fail-under=85 > /dev/null || exit 1; \
	done

lint: ## ruff, ruff format --check, mypy --strict
	$(BIN)/ruff check .
	$(BIN)/ruff format --check .
	$(BIN)/mypy

security: ## bandit, pip-audit, gitleaks
	$(BIN)/bandit -q -r src/sentinel
	$(BIN)/pip-audit --progress-spinner off --require-hashes --disable-pip -r requirements/dev.txt
	gitleaks git --redact --no-banner .

migrate: ## alembic upgrade head (dev stack)
	docker compose run --rm migrate

catalog: ## Validate catalog packs and show the import plan against the dev stack
	PYTHONPATH=src $(BIN)/python -m sentinel.catalog validate
	docker compose run --rm migrate python -m sentinel.catalog import

catalog-schema: ## Regenerate catalog/pack.schema.json from the models
	PYTHONPATH=src $(BIN)/python -m sentinel.catalog schema > catalog/pack.schema.json

check: lint security test ## Everything CI runs; must pass before every commit
