.PHONY: help sync lock-check lint format-check docs-check migration-check naming-check architecture-check test-libraries test-scrapy-collector execution-coordination-tests acquisition-output-tests acquisition-output-fixture-example acquisition-output-mongo-setup acquisition-output-mongo-check-config acquisition-output-mongo-up acquisition-output-mongo-status acquisition-output-mongo-connectivity acquisition-output-mongo-provision acquisition-output-mongo-tests acquisition-output-mongo-example acquisition-output-mongo-backup-restore acquisition-output-mongo-benchmark acquisition-output-mongo-benchmark-100k acquisition-output-mongo-down scrapy-collector-multibot-conformance execution-coordination-protected-e2e test check hooks-install pre-commit ci db-up db-status db-migrate db-down test-integration test-recovery scout-relay scout-relay-once scout-sandbox-check scout-http-e2e scout-dns-adversarial-check scout-protected-replay-check scout-dev-setup scout-dev-status scout-dev-authorize-single-fixture scout-authoring-acceptance collector-playbook-examples collector-cross-backend-conformance

COMPOSE_FILE := deployments/compose/postgres.compose.yaml
MONGO_COMPOSE_FILE := deployments/compose/mongodb.compose.yaml

define require_env
	@test -f .env || { echo 'Copy .env.example to .env and review local-only credentials.'; exit 2; }
endef

help:
	@printf '%s\n' 'sync          Install locked development dependencies' 'check         Run the fast quality gate' 'acquisition-output-tests Run BC05 output domain and contract tests' 'acquisition-output-fixture-example Run the synthetic worker-independent output example' 'db-up         Start local PostgreSQL and wait for readiness' 'db-migrate    Apply checksummed pending Scout migrations' 'test-integration Run PostgreSQL integration/recovery tests' 'test-recovery Alias for the PostgreSQL integration/recovery suite' 'scout-relay   Run independent Scout outbox worker' 'scout-sandbox-check Verify optional network egress isolation with Docker' 'scout-protected-replay-check Run the protected BC15 executor E2E' 'scout-http-e2e Run MCP/HTTPS tests (not DNS adversarial tests)' 'scout-dns-adversarial-check Run Squid/DNS tests through the real proxy' 'db-down       Stop PostgreSQL without deleting its volume' 'ci            Run repository checks'

sync:
	uv sync --all-packages --locked

lock-check:
	uv lock --check

lint:
	uv run --locked ruff check tooling tests apps/scout apps/collector-engineering apps/workers/scrapy-collector apps/execution-coordination apps/acquisition-output libs/python/bot-build libs/python/observability

format-check:
	uv run --locked ruff format --check tooling tests apps/scout apps/collector-engineering apps/workers/scrapy-collector apps/execution-coordination apps/acquisition-output libs/python/bot-build libs/python/observability

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
	uv run --locked --package mozaic-bot-build python -m unittest discover -s libs/python/bot-build/tests -v

test-scrapy-collector:
	uv run --locked --package mozaic-scrapy-collector python -m unittest discover -s apps/workers/scrapy-collector/tests -v

execution-coordination-tests:
	uv run --locked --package mozaic-execution-coordination python -m unittest discover -s apps/execution-coordination/tests -v

acquisition-output-tests:
	uv run --locked --package mozaic-acquisition-output python -m unittest discover -s apps/acquisition-output/tests -v

acquisition-output-fixture-example:
	uv run --locked --package mozaic-acquisition-output python examples/acquisition-output-fixture/consume.py

acquisition-output-mongo-setup:
	uv run --locked python tooling/mongodb_local_setup.py

acquisition-output-mongo-check-config: acquisition-output-mongo-setup
	@test -f .env.mongodb || { echo 'Run make acquisition-output-mongo-setup first.'; exit 2; }
	@set -a; . .env.mongodb; set +a; uv run --locked --all-packages python -m acquisition_output.adapters.mongodb.cli check-config

acquisition-output-mongo-up: acquisition-output-mongo-check-config
	docker compose --env-file .env.mongodb -f $(MONGO_COMPOSE_FILE) up --wait -d mongodb
	docker compose --env-file .env.mongodb -f $(MONGO_COMPOSE_FILE) run --rm mongodb-init

acquisition-output-mongo-status:
	docker compose --env-file .env.mongodb -f $(MONGO_COMPOSE_FILE) ps

acquisition-output-mongo-connectivity: acquisition-output-mongo-up
	@set -a; . .env.mongodb; set +a; uv run --locked --all-packages python -m acquisition_output.adapters.mongodb.cli ping
	docker compose --env-file .env.mongodb -f $(MONGO_COMPOSE_FILE) exec mongodb bash -ec 'mongosh --quiet --host mongodb:27017 --username "$$MONGO_INITDB_ROOT_USERNAME" --password "$$MONGO_INITDB_ROOT_PASSWORD" --authenticationDatabase admin --eval '\''db.hello().setName'\'''

acquisition-output-mongo-provision: acquisition-output-mongo-up
	@set -a; . .env.mongodb; set +a; uv run --locked --all-packages python -m acquisition_output.adapters.mongodb.cli provision

acquisition-output-mongo-tests: acquisition-output-mongo-provision
	@set -a; . .env.mongodb; set +a; uv run --locked --all-packages python -m unittest discover -s apps/acquisition-output/integration_tests -v

acquisition-output-mongo-example: acquisition-output-mongo-provision
	@set -a; . .env.mongodb; set +a; uv run --locked --all-packages python examples/acquisition-output-mongodb/consume.py

acquisition-output-mongo-backup-restore: acquisition-output-mongo-provision
	@set -a; . .env.mongodb; set +a; uv run --locked --all-packages python tooling/mongodb_backup_restore_smoke.py

acquisition-output-mongo-benchmark: acquisition-output-mongo-provision
	@set -a; . .env.mongodb; set +a; uv run --locked --all-packages python tooling/benchmark_acquisition_output_mongodb.py

acquisition-output-mongo-benchmark-100k: acquisition-output-mongo-provision
	@set -a; . .env.mongodb; set +a; uv run --locked --all-packages python tooling/benchmark_acquisition_output_mongodb.py --counts=100000

acquisition-output-mongo-down:
	docker compose --env-file .env.mongodb -f $(MONGO_COMPOSE_FILE) down

execution-coordination-protected-e2e:
	uv run --locked --all-packages python tooling/run_execution_coordination_e2e.py --docker

test:
	uv run --locked python -m unittest discover -s tests -v

check: lock-check lint format-check docs-check migration-check naming-check architecture-check test-libraries test-scrapy-collector execution-coordination-tests acquisition-output-tests test

hooks-install: sync
	uv run --locked pre-commit install --hook-type pre-commit
	uv run --locked pre-commit install --hook-type commit-msg

pre-commit: sync
	uv run --locked pre-commit run --all-files

ci: sync check

collector-playbook-examples:
	uv run --locked --package mozaic-collector-engineering python -m collector_engineering verify-network-sandbox
	uv run --locked --package mozaic-collector-engineering python -m collector_engineering run-example examples/playbooks/html-simple
	uv run --locked --package mozaic-collector-engineering python -m collector_engineering run-example examples/playbooks/multipage-catalog
	uv run --locked --package mozaic-collector-engineering python -m collector_engineering run-example examples/playbooks/json-api
	uv run --locked --package mozaic-collector-engineering python -m collector_engineering run-example examples/playbooks/browser-capability-unsupported
	uv run --locked --package mozaic-collector-engineering python -m collector_engineering run-example examples/playbooks/codex-authorship-demo

collector-cross-backend-conformance:
	uv run --locked --package mozaic-collector-engineering python -m collector_engineering cross-backend-conformance

scrapy-collector-multibot-conformance:
	uv run --locked --all-packages python tooling/run_scrapy_collector_multibot.py --docker

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
	@set -a; . ./.env; set +a; test -n "$$SCOUT_DATABASE_URL" || { echo 'SCOUT_DATABASE_URL is required; refusing a skipped integration suite.'; exit 2; }; SCOUT_TEST_DATABASE_URL="$$SCOUT_DATABASE_URL" uv run --locked python tooling/run_postgres_integration.py

test-recovery: test-integration

scout-relay:
	$(require_env)
	@set -a; . ./.env; set +a; PYTHONPATH=apps/scout uv run --locked python -m scout.relay_worker

scout-relay-once:
	$(require_env)
	@set -a; . ./.env; set +a; PYTHONPATH=apps/scout uv run --locked python -m scout.relay_worker --once

scout-sandbox-check:
	uv run --locked python tooling/check_scout_sandbox.py

scout-protected-replay-check:
	uv run --locked python tooling/run_scout_protected_replay_check.py

scout-http-e2e:
	$(require_env)
	@set -a; . ./.env; set +a; uv run --locked python tooling/run_scout_http_e2e.py --suite http

scout-dns-adversarial-check:
	$(require_env)
	@set -a; . ./.env; set +a; uv run --locked python tooling/run_scout_http_e2e.py --suite dns

scout-dev-setup:
	python3 tooling/scout_dev.py setup

scout-dev-status:
	python3 tooling/scout_dev.py status

scout-dev-authorize-single-fixture:
	python3 tooling/scout_dev.py authorize-single-fixture

scout-authoring-acceptance:
	@test -x .venv/bin/python || { echo 'Run make scout-dev-setup first.'; exit 2; }
	.venv/bin/python tooling/scout_dev.py accept-single-fixture
