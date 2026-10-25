SHELL := /bin/bash
COMPOSE := docker compose
TEST_DATABASE_URL ?= postgresql+asyncpg://jarvis_test:jarvis_test@localhost:55432/jarvis_test

.PHONY: help bootstrap up down restart status watch logs ps migrate doctor test test-db test-live lint typecheck fmt check backup restore \
        backup-restore-test build dev-worker

help:
	@grep -E '^[a-z-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS=":.*?## "}; {printf "%-22s %s\n", $$1, $$2}'

bootstrap: ## install Python deps (uv) and create .env from the example if missing
	uv python install 3.12
	uv sync --all-extras
	@test -f .env || (cp .env.example .env && echo "created .env — fill in secrets (see README)")

build: ## build the application image
	$(COMPOSE) build

up: ## start postgres + migrate + api/worker/scheduler/bot
	$(COMPOSE) up -d --build

down: ## stop all services (data volume is kept)
	$(COMPOSE) down

restart: ## restart app services (e.g. after editing .env)
	$(COMPOSE) up -d --build

status: ## readable snapshot: services, next reminders, what was sent, sync times, errors
	./scripts/status.sh

watch: ## live readable activity feed (Ctrl+C to stop)
	./scripts/watch.sh

logs: ## follow logs
	$(COMPOSE) logs -f --tail=200

ps: ## service status
	$(COMPOSE) ps

migrate: ## apply migrations inside the stack
	$(COMPOSE) run --rm migrate

doctor: ## diagnostics (no secrets printed, no billable calls)
	$(COMPOSE) run --rm api jarvis doctor

db: ## start only postgres (for local tests)
	$(COMPOSE) up -d postgres

test-db: ## once on a fresh checkout: test role jarvis_test + database jarvis_test (pgvector in schema extensions)
	$(COMPOSE) up -d --wait postgres
	./scripts/db_test_role.sh

test: db ## lint + typecheck + full normal test suite (no network, no paid calls)
	uv run ruff check .
	uv run mypy
	TEST_DATABASE_URL=$(TEST_DATABASE_URL) uv run pytest

test-live: ## opt-in live tests (needs JARVIS_LIVE_TESTS=true and credentials)
	uv run pytest tests/live -o addopts="" -v

lint: ## ruff lint
	uv run ruff check .

fmt: ## format
	uv run ruff format . && uv run ruff check --fix .

typecheck: ## mypy
	uv run mypy

backup: ## pg_dump into ./backups
	./scripts/backup.sh

restore: ## restore: make restore FILE=backups/xxx.dump
	./scripts/restore.sh $(FILE)

backup-restore-test: ## verify a backup restores into a scratch database
	./scripts/test_backup_restore.sh

dev-worker: ## run the Dev Worker on the development machine
	uv run jarvis-dev-worker run
