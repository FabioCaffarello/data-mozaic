.PHONY: help sync lock-check lint format-check docs-check migration-check naming-check architecture-check test-libraries test check hooks-install pre-commit ci db-up db-status db-migrate db-down test-integration test-recovery scout-relay scout-relay-once

COMPOSE_FILE := deployments/compose/postgres.compose.yaml

define require_env
	@test -f .env || { echo 'Copy .env.example to .env and review local-only credentials.'; exit 2; }
endef

help:
	@printf '%s\n' 'sync          Install locked development dependencies' 'check         Run repository checks' 'db-up         Start local PostgreSQL and wait for readiness' 'db-migrate    Apply checksummed pending Scout migrations' 'test-integration Run PostgreSQL recovery tests in an isolated database' 'scout-relay   Run independent Scout outbox worker' 'db-down       Stop PostgreSQL without deleting its volume' 'ci            Run repository checks'

sync:
	uv sync --all-packages --locked

lock-check:
	uv lock --check

lint:
	uv run --locked ruff check tooling tests apps/scout apps/collector-engineering libs/python/observability

format-check:
	uv run --locked ruff format --check tooling tests apps/scout apps/collector-engineering libs/python/observability

docs-check:
	uv run --locked python tooling/check_docs.py

migration-check:
	uv run --locked python tooling/check_migrations.py

naming-check:
	uv run --locked python tooling/check_naming.py

architecture-check:
	uv run --locked python tooling/check_architecture.py

test-libraries:
	uv run --locked --package mozaic-observability python -m unittest discover -s libs/python/observability/tests -v

test:
	uv run --locked python -m unittest discover -s tests -v

check: lock-check lint format-check docs-check migration-check naming-check architecture-check test-libraries test

hooks-install: sync
	uv run --locked pre-commit install --hook-type pre-commit
	uv run --locked pre-commit install --hook-type commit-msg

pre-commit: sync
	uv run --locked pre-commit run --all-files

ci: sync check

db-up:
	$(require_env)
	docker compose --env-file .env -f $(COMPOSE_FILE) up --wait -d

db-status:
	$(require_env)
	docker compose --env-file .env -f $(COMPOSE_FILE) ps

db-migrate:
	$(require_env)
	@set -a; . ./.env; set +a; PYTHONPATH=apps/scout uv run --locked python tooling/db_migrate.py

db-down:
	$(require_env)
	docker compose --env-file .env -f $(COMPOSE_FILE) down

test-integration:
	$(require_env)
	@set -a; . ./.env; set +a; SCOUT_TEST_DATABASE_URL="$$SCOUT_DATABASE_URL" uv run --locked python -m unittest discover -s tests/integration -v

test-recovery: test-integration

scout-relay:
	$(require_env)
	@set -a; . ./.env; set +a; PYTHONPATH=apps/scout uv run --locked python -m scout.relay_worker

scout-relay-once:
	$(require_env)
	@set -a; . ./.env; set +a; PYTHONPATH=apps/scout uv run --locked python -m scout.relay_worker --once
