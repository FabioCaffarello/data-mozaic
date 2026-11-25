.PHONY: help sync lock-check lint format-check docs-check migration-check naming-check architecture-check test-libraries test-scrapy-collector collector-live-admission-tests execution-coordination-tests execution-coordination-durable-setup execution-coordination-durable-migrate execution-coordination-durable-tests execution-coordination-finalization-tests execution-coordination-example execution-coordination-reconcile execution-coordination-recover-run authorized-live-json-acquisition authorized-live-html-acquisition authorized-live-pagination-acquisition authorized-live-pagination-planned-acquisition authorized-live-redirect-acquisition authorized-live-redirect-denial authorized-live-redirect-budget-exhaustion authorized-live-publication-ack-loss authorized-live-pagination-cancellation authorized-live-custody-interruption authorized-live-partial-stage-interruption acquisition-output-tests acquisition-application-tests acquisition-output-fixture-example acquisition-output-mongo-setup acquisition-output-mongo-check-config acquisition-output-mongo-up acquisition-output-mongo-status acquisition-output-mongo-connectivity acquisition-output-mongo-provision acquisition-output-mongo-tests acquisition-output-mongo-example acquisition-output-mongo-backup-restore acquisition-output-mongo-benchmark acquisition-output-mongo-benchmark-100k acquisition-output-mongo-down artifact-custody-setup artifact-custody-go-format-check artifact-custody-go-vet artifact-custody-go-test artifact-custody-go-race artifact-custody-go-build artifact-custody-go-vuln acquisition-gateway-go-format-check acquisition-gateway-go-vet acquisition-gateway-go-test acquisition-gateway-go-race acquisition-gateway-go-build acquisition-gateway-go-vuln acquisition-live-gateway-container-smoke artifact-custody-up artifact-custody-status artifact-custody-down artifact-custody-integration artifact-custody-db-bootstrap artifact-custody-object-provision artifact-custody-reconcile artifact-custody-recovery-tests artifact-custody-benchmark artifact-custody-backup-restore scrapy-collector-multibot-conformance execution-coordination-protected-e2e test check hooks-install pre-commit ci db-up db-status db-migrate db-down test-integration test-recovery scout-relay scout-relay-once scout-sandbox-check scout-http-e2e scout-dns-adversarial-check scout-protected-replay-check scout-dev-setup scout-dev-status scout-dev-authorize-single-fixture scout-authoring-acceptance collector-playbook-examples collector-cross-backend-conformance
.PHONY: authorized-live-scenario
COMPOSE_FILE := deployments/compose/postgres.compose.yaml
MONGO_COMPOSE_FILE := deployments/compose/mongodb.compose.yaml
BC06_COMPOSE_FILE := deployments/compose/artifact-custody.compose.yaml
BC06_ENV_FILE ?= $(if $(wildcard .env.artifact-custody),.env.artifact-custody,../data-mozaic-artifact-custody/.env.artifact-custody)
MONGO_ENV_FILE := $(if $(wildcard .env.mongodb),.env.mongodb,$(if $(wildcard ../data-mozaic-mongodb/.env.mongodb),../data-mozaic-mongodb/.env.mongodb,.env.mongodb))
GO ?= go
GO_TOOLCHAIN := GOTOOLCHAIN=go1.27.4

define require_env
	@test -f .env || { echo 'Copy .env.example to .env and review local-only credentials.'; exit 2; }
endef

help:
	@printf '%s\n' 'sync          Install locked development dependencies' 'check         Run the fast quality gate' 'acquisition-output-tests Run BC05 output domain and contract tests' 'acquisition-output-fixture-example Run the synthetic worker-independent output example' 'execution-coordination-fixture-acquisition Run durable fixture-only Scrapy through BC04/BC05/BC06' 'execution-coordination-recover-run Recover one expired BC04 run by tenant and run ID' 'db-up         Start local PostgreSQL and wait for readiness' 'db-migrate    Apply checksummed pending Scout migrations' 'test-integration Run PostgreSQL integration/recovery tests' 'test-recovery Alias for the PostgreSQL integration/recovery suite' 'scout-relay   Run independent Scout outbox worker' 'scout-sandbox-check Verify optional network egress isolation with Docker' 'scout-protected-replay-check Run the protected BC15 executor E2E' 'scout-http-e2e Run MCP/HTTPS tests (not DNS adversarial tests)' 'scout-dns-adversarial-check Run Squid/DNS tests through the real proxy' 'db-down       Stop PostgreSQL without deleting its volume' 'ci            Run repository checks'

sync:
	uv sync --all-packages --locked

lock-check:
	uv lock --check

lint:
	uv run --locked ruff check tooling tests apps/scout apps/collector-engineering apps/workers/scrapy-collector apps/execution-coordination apps/acquisition-output apps/acquisition-application libs/python/bot-build libs/python/observability

format-check:
	uv run --locked ruff format --check tooling tests apps/scout apps/collector-engineering apps/workers/scrapy-collector apps/execution-coordination apps/acquisition-output apps/acquisition-application libs/python/bot-build libs/python/observability

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

collector-live-admission-tests:
	uv run --locked --package mozaic-collector-engineering python -m unittest discover -s apps/collector-engineering/tests -v

execution-coordination-tests:
	uv run --locked --package mozaic-execution-coordination python -m unittest discover -s apps/execution-coordination/tests -v

execution-coordination-durable-setup:
	@set -a; if [ -f .env ]; then . ./.env; fi; set +a; uv run --locked --package mozaic-execution-coordination python tooling/execution_coordination_local_setup.py

execution-coordination-durable-migrate: execution-coordination-durable-setup
	@set -a; . ./.env.execution-coordination; set +a; uv run --locked --package mozaic-execution-coordination python tooling/execution_coordination_migrate.py

execution-coordination-durable-tests: execution-coordination-durable-migrate
	@set -a; . ./.env.execution-coordination; set +a; uv run --locked --package mozaic-execution-coordination python -m unittest discover -s apps/execution-coordination/integration_tests -v

execution-coordination-finalization-tests: execution-coordination-durable-tests
	@set -a; . ./.env.execution-coordination; . ./$(MONGO_ENV_FILE); set +a; uv run --locked --all-packages python -m acquisition_output.adapters.mongodb.cli ping
	@set -a; . ./.env.execution-coordination; . ./$(MONGO_ENV_FILE); set +a; uv run --locked --all-packages python -m acquisition_output.adapters.mongodb.cli provision
	@test -f $(BC06_ENV_FILE) || { echo 'Use the preserved BC06 local environment and make artifact-custody-up from its worktree first.'; exit 2; }
	@set -a; . ./.env.execution-coordination; . ./$(MONGO_ENV_FILE); . ./$(BC06_ENV_FILE); set +a; ARTIFACT_CUSTODY_HTTP_URL=http://127.0.0.1:8086 uv run --locked --all-packages python tooling/execution_coordination_finalization_integration.py

execution-coordination-example: execution-coordination-finalization-tests

execution-coordination-reconcile: execution-coordination-durable-migrate
	@set -a; . ./.env.execution-coordination; . ./$(MONGO_ENV_FILE); set +a; BC04_LOCAL_FIXTURE_PROFILE=1 uv run --locked --all-packages python tooling/execution_coordination_reconcile.py

execution-coordination-recover-run: execution-coordination-durable-migrate
	@test -n "$(TENANT_ID)" -a -n "$(RUN_ID)" || { echo 'Set TENANT_ID and RUN_ID to scope BC04 recovery.'; exit 2; }
	@set -a; . ./.env.execution-coordination; set +a; uv run --locked --package mozaic-execution-coordination python tooling/execution_coordination_recover_run.py --tenant-id '$(TENANT_ID)' --run-id '$(RUN_ID)'

execution-coordination-fixture-acquisition: execution-coordination-durable-migrate
	@test -f $(MONGO_ENV_FILE) || { echo 'Provide the preserved local MongoDB environment.'; exit 2; }
	@test -f $(BC06_ENV_FILE) || { echo 'Start BC06 using its preserved local environment and set BC06_ENV_FILE.'; exit 2; }
	@set -a; . ./.env.execution-coordination; . ./$(MONGO_ENV_FILE); set +a; uv run --locked --all-packages python -m acquisition_output.adapters.mongodb.cli ping
	@set -a; . ./.env.execution-coordination; . ./$(MONGO_ENV_FILE); set +a; uv run --locked --all-packages python -m acquisition_output.adapters.mongodb.cli provision
	@set -a; . ./.env.execution-coordination; . ./$(MONGO_ENV_FILE); . ./$(BC06_ENV_FILE); set +a; BC04_LOCAL_FIXTURE_PROFILE=1 ARTIFACT_CUSTODY_HTTP_URL=http://127.0.0.1:8086 uv run --locked --all-packages python tooling/run_durable_fixture_acquisition.py

authorized-live-json-acquisition: execution-coordination-durable-migrate
	@test -f $(MONGO_ENV_FILE) -a -f $(BC06_ENV_FILE) || { echo 'Required preserved MongoDB and BC06 environment files are unavailable.'; exit 2; }
	@set -a; . ./.env.execution-coordination; . ./$(MONGO_ENV_FILE); set +a; uv run --locked --all-packages python -m acquisition_output.adapters.mongodb.cli ping
	@set -a; . ./.env.execution-coordination; . ./$(MONGO_ENV_FILE); set +a; uv run --locked --all-packages python -m acquisition_output.adapters.mongodb.cli provision
	@set -a; . ./.env.execution-coordination; . ./$(MONGO_ENV_FILE); . ./$(BC06_ENV_FILE); set +a; ARTIFACT_CUSTODY_HTTP_URL=http://127.0.0.1:8086 uv run --locked --all-packages python tooling/run_authorized_live_json.py

authorized-live-html-acquisition: execution-coordination-durable-migrate
	@test -f $(MONGO_ENV_FILE) -a -f $(BC06_ENV_FILE) || { echo 'Required preserved MongoDB and BC06 environment files are unavailable.'; exit 2; }
	@set -a; . ./.env.execution-coordination; . ./$(MONGO_ENV_FILE); set +a; uv run --locked --all-packages python -m acquisition_output.adapters.mongodb.cli ping
	@set -a; . ./.env.execution-coordination; . ./$(MONGO_ENV_FILE); set +a; uv run --locked --all-packages python -m acquisition_output.adapters.mongodb.cli provision
	@set -a; . ./.env.execution-coordination; . ./$(MONGO_ENV_FILE); . ./$(BC06_ENV_FILE); set +a; ARTIFACT_CUSTODY_HTTP_URL=http://127.0.0.1:8086 uv run --locked --all-packages python tooling/run_authorized_live_json.py --case html

authorized-live-pagination-acquisition: execution-coordination-durable-migrate
	@test -f $(MONGO_ENV_FILE) -a -f $(BC06_ENV_FILE) || { echo 'Required preserved MongoDB and BC06 environment files are unavailable.'; exit 2; }
	@set -a; . ./.env.execution-coordination; . ./$(MONGO_ENV_FILE); set +a; uv run --locked --all-packages python -m acquisition_output.adapters.mongodb.cli ping
	@set -a; . ./.env.execution-coordination; . ./$(MONGO_ENV_FILE); set +a; uv run --locked --all-packages python -m acquisition_output.adapters.mongodb.cli provision
	@set -a; . ./.env.execution-coordination; . ./$(MONGO_ENV_FILE); . ./$(BC06_ENV_FILE); set +a; ARTIFACT_CUSTODY_HTTP_URL=http://127.0.0.1:8086 uv run --locked --all-packages python tooling/run_authorized_live_json.py --case pagination

authorized-live-pagination-planned-acquisition: execution-coordination-durable-migrate
	@test -f $(MONGO_ENV_FILE) -a -f $(BC06_ENV_FILE) || { echo 'Required preserved MongoDB and BC06 environment files are unavailable.'; exit 2; }
	@set -a; . ./.env.execution-coordination; . ./$(MONGO_ENV_FILE); set +a; uv run --locked --all-packages python -m acquisition_output.adapters.mongodb.cli ping
	@set -a; . ./.env.execution-coordination; . ./$(MONGO_ENV_FILE); set +a; uv run --locked --all-packages python -m acquisition_output.adapters.mongodb.cli provision
	@set -a; . ./.env.execution-coordination; . ./$(MONGO_ENV_FILE); . ./$(BC06_ENV_FILE); set +a; ARTIFACT_CUSTODY_HTTP_URL=http://127.0.0.1:8086 uv run --locked --all-packages python tooling/run_authorized_live_json.py --case pagination-planned

authorized-live-redirect-acquisition: execution-coordination-durable-migrate
	@test -f $(MONGO_ENV_FILE) -a -f $(BC06_ENV_FILE) || { echo 'Required preserved MongoDB and BC06 environment files are unavailable.'; exit 2; }
	@set -a; . ./.env.execution-coordination; . ./$(MONGO_ENV_FILE); set +a; uv run --locked --all-packages python -m acquisition_output.adapters.mongodb.cli ping
	@set -a; . ./.env.execution-coordination; . ./$(MONGO_ENV_FILE); set +a; uv run --locked --all-packages python -m acquisition_output.adapters.mongodb.cli provision
	@set -a; . ./.env.execution-coordination; . ./$(MONGO_ENV_FILE); . ./$(BC06_ENV_FILE); set +a; ARTIFACT_CUSTODY_HTTP_URL=http://127.0.0.1:8086 uv run --locked --all-packages python tooling/run_authorized_live_json.py --case redirect

authorized-live-redirect-denial: execution-coordination-durable-migrate
	@test -f $(MONGO_ENV_FILE) -a -f $(BC06_ENV_FILE) || { echo 'Required preserved MongoDB and BC06 environment files are unavailable.'; exit 2; }
	@set -a; . ./.env.execution-coordination; . ./$(MONGO_ENV_FILE); set +a; uv run --locked --all-packages python -m acquisition_output.adapters.mongodb.cli ping
	@set -a; . ./.env.execution-coordination; . ./$(MONGO_ENV_FILE); set +a; uv run --locked --all-packages python -m acquisition_output.adapters.mongodb.cli provision
	@set -a; . ./.env.execution-coordination; . ./$(MONGO_ENV_FILE); . ./$(BC06_ENV_FILE); set +a; ARTIFACT_CUSTODY_HTTP_URL=http://127.0.0.1:8086 uv run --locked --all-packages python tooling/run_authorized_live_json.py --case redirect-denied

authorized-live-redirect-budget-exhaustion: execution-coordination-durable-migrate
	@test -f $(MONGO_ENV_FILE) -a -f $(BC06_ENV_FILE) || { echo 'Required preserved MongoDB and BC06 environment files are unavailable.'; exit 2; }
	@set -a; . ./.env.execution-coordination; . ./$(MONGO_ENV_FILE); set +a; uv run --locked --all-packages python -m acquisition_output.adapters.mongodb.cli ping
	@set -a; . ./.env.execution-coordination; . ./$(MONGO_ENV_FILE); set +a; uv run --locked --all-packages python -m acquisition_output.adapters.mongodb.cli provision
	@set -a; . ./.env.execution-coordination; . ./$(MONGO_ENV_FILE); . ./$(BC06_ENV_FILE); set +a; ARTIFACT_CUSTODY_HTTP_URL=http://127.0.0.1:8086 uv run --locked --all-packages python tooling/run_authorized_live_json.py --case redirect-budget

authorized-live-publication-ack-loss: execution-coordination-durable-migrate
	@test -f $(MONGO_ENV_FILE) -a -f $(BC06_ENV_FILE) || { echo 'Required preserved MongoDB and BC06 environment files are unavailable.'; exit 2; }
	@set -a; . ./.env.execution-coordination; . ./$(MONGO_ENV_FILE); set +a; uv run --locked --all-packages python -m acquisition_output.adapters.mongodb.cli ping
	@set -a; . ./.env.execution-coordination; . ./$(MONGO_ENV_FILE); set +a; uv run --locked --all-packages python -m acquisition_output.adapters.mongodb.cli provision
	@set -a; . ./.env.execution-coordination; . ./$(MONGO_ENV_FILE); . ./$(BC06_ENV_FILE); set +a; ARTIFACT_CUSTODY_HTTP_URL=http://127.0.0.1:8086 uv run --locked --all-packages python tooling/run_authorized_live_json.py --case ack-loss

authorized-live-pagination-cancellation: execution-coordination-durable-migrate
	@test -f $(MONGO_ENV_FILE) -a -f $(BC06_ENV_FILE) || { echo 'Required preserved MongoDB and BC06 environment files are unavailable.'; exit 2; }
	@set -a; . ./.env.execution-coordination; . ./$(MONGO_ENV_FILE); set +a; uv run --locked --all-packages python -m acquisition_output.adapters.mongodb.cli ping
	@set -a; . ./.env.execution-coordination; . ./$(MONGO_ENV_FILE); set +a; uv run --locked --all-packages python -m acquisition_output.adapters.mongodb.cli provision
	@set -a; . ./.env.execution-coordination; . ./$(MONGO_ENV_FILE); . ./$(BC06_ENV_FILE); set +a; ARTIFACT_CUSTODY_HTTP_URL=http://127.0.0.1:8086 uv run --locked --all-packages python tooling/run_authorized_live_json.py --case pagination-cancel

authorized-live-custody-interruption: execution-coordination-durable-migrate
	@test -f $(MONGO_ENV_FILE) -a -f $(BC06_ENV_FILE) || { echo 'Required preserved MongoDB and BC06 environment files are unavailable.'; exit 2; }
	@set -a; . ./.env.execution-coordination; . ./$(MONGO_ENV_FILE); set +a; uv run --locked --all-packages python -m acquisition_output.adapters.mongodb.cli ping
	@set -a; . ./.env.execution-coordination; . ./$(MONGO_ENV_FILE); set +a; uv run --locked --all-packages python -m acquisition_output.adapters.mongodb.cli provision
	@set -a; . ./.env.execution-coordination; . ./$(MONGO_ENV_FILE); . ./$(BC06_ENV_FILE); set +a; ARTIFACT_CUSTODY_HTTP_URL=http://127.0.0.1:8086 uv run --locked --all-packages python tooling/run_authorized_live_json.py --case custody-interruption

authorized-live-partial-stage-interruption: execution-coordination-durable-migrate
	@test -f $(MONGO_ENV_FILE) -a -f $(BC06_ENV_FILE) || { echo 'Required preserved MongoDB and BC06 environment files are unavailable.'; exit 2; }
	@set -a; . ./.env.execution-coordination; . ./$(MONGO_ENV_FILE); set +a; uv run --locked --all-packages python -m acquisition_output.adapters.mongodb.cli ping
	@set -a; . ./.env.execution-coordination; . ./$(MONGO_ENV_FILE); set +a; uv run --locked --all-packages python -m acquisition_output.adapters.mongodb.cli provision
	@set -a; . ./.env.execution-coordination; . ./$(MONGO_ENV_FILE); . ./$(BC06_ENV_FILE); set +a; ARTIFACT_CUSTODY_HTTP_URL=http://127.0.0.1:8086 uv run --locked --all-packages python tooling/run_authorized_live_json.py --case partial-stage-interruption

authorized-live-scenario: execution-coordination-durable-migrate
	@test -n "$(CASE)" || { echo 'Set CASE to one controlled response-failure scenario.'; exit 2; }
	@test -f $(MONGO_ENV_FILE) -a -f $(BC06_ENV_FILE) || { echo 'Required preserved MongoDB and BC06 environment files are unavailable.'; exit 2; }
	@set -a; . ./.env.execution-coordination; . ./$(MONGO_ENV_FILE); set +a; uv run --locked --all-packages python -m acquisition_output.adapters.mongodb.cli ping
	@set -a; . ./.env.execution-coordination; . ./$(MONGO_ENV_FILE); set +a; uv run --locked --all-packages python -m acquisition_output.adapters.mongodb.cli provision
	@set -a; . ./.env.execution-coordination; . ./$(MONGO_ENV_FILE); . ./$(BC06_ENV_FILE); set +a; ARTIFACT_CUSTODY_HTTP_URL=http://127.0.0.1:8086 uv run --locked --all-packages python tooling/run_authorized_live_json.py --case "$(CASE)"

acquisition-output-tests:
	uv run --locked --package mozaic-acquisition-output python -m unittest discover -s apps/acquisition-output/tests -v

acquisition-application-tests:
	uv run --locked --package mozaic-trusted-acquisition-application python -m unittest discover -s apps/acquisition-application/tests -v

artifact-custody-setup:
	uv run --locked python tooling/artifact_custody_local_setup.py

artifact-custody-go-format-check:
	@test -z "$$($(GO_TOOLCHAIN) $(GO)fmt -l apps/artifact-custody)" || { $(GO_TOOLCHAIN) $(GO)fmt -l apps/artifact-custody; exit 1; }

artifact-custody-go-vet:
	cd apps/artifact-custody && $(GO_TOOLCHAIN) $(GO) vet ./...

artifact-custody-go-test:
	cd apps/artifact-custody && $(GO_TOOLCHAIN) $(GO) test ./...

artifact-custody-go-race:
	cd apps/artifact-custody && $(GO_TOOLCHAIN) $(GO) test -race ./...

artifact-custody-go-build:
	cd apps/artifact-custody && $(GO_TOOLCHAIN) env GOOS=linux GOARCH=amd64 CGO_ENABLED=0 $(GO) build ./cmd/...

artifact-custody-go-vuln:
	cd apps/artifact-custody && $(GO_TOOLCHAIN) $(GO) run golang.org/x/vuln/cmd/govulncheck@v1.10.0 ./...

acquisition-gateway-go-format-check:
	@test -z "$$(cd apps/acquisition-gateway && $(GO_TOOLCHAIN) $(GO)fmt -l cmd internal)" || { cd apps/acquisition-gateway && $(GO_TOOLCHAIN) $(GO)fmt -l cmd internal; exit 1; }

acquisition-gateway-go-vet:
	cd apps/acquisition-gateway && $(GO_TOOLCHAIN) $(GO) vet ./...

acquisition-gateway-go-test:
	cd apps/acquisition-gateway && $(GO_TOOLCHAIN) $(GO) test ./...

acquisition-gateway-go-race:
	cd apps/acquisition-gateway && $(GO_TOOLCHAIN) $(GO) test -race ./...

acquisition-gateway-go-build:
	cd apps/acquisition-gateway && $(GO_TOOLCHAIN) env GOOS=linux GOARCH=amd64 CGO_ENABLED=0 $(GO) build ./cmd/...

acquisition-gateway-go-vuln:
	cd apps/acquisition-gateway && $(GO_TOOLCHAIN) $(GO) run golang.org/x/vuln/cmd/govulncheck@v1.10.0 ./...

acquisition-live-gateway-container-smoke:
	uv run --locked --all-packages python tooling/acquisition_live_gateway_smoke.py

artifact-custody-db-bootstrap: artifact-custody-setup
	@test -f .env.artifact-custody || { echo 'Run make artifact-custody-setup first.'; exit 2; }
	@set -a; . ./.env.artifact-custody; set +a; docker exec -i -e BC06_APP_PASSWORD="$$BC06_APP_DB_PASSWORD" -e BC06_OWNER_PASSWORD="$$BC06_OWNER_DB_PASSWORD" data-mozaic-scout-local-postgres-1 sh -ec 'psql -v ON_ERROR_STOP=1 -U "$$POSTGRES_USER" -d "$$POSTGRES_DB" -v app_user=bc06_app -v app_password="$$BC06_APP_PASSWORD" -v owner_user=bc06_owner -v owner_password="$$BC06_OWNER_PASSWORD" -f -' < tooling/artifact_custody_db_bootstrap.sql

artifact-custody-object-provision: artifact-custody-setup
	docker compose --env-file .env.artifact-custody -f $(BC06_COMPOSE_FILE) run --build --rm rustfs-provision

artifact-custody-up: artifact-custody-db-bootstrap
	docker compose --env-file .env.artifact-custody -f $(BC06_COMPOSE_FILE) up --wait -d rustfs
	docker compose --env-file .env.artifact-custody -f $(BC06_COMPOSE_FILE) run --build --rm rustfs-provision
	docker compose --env-file .env.artifact-custody -f $(BC06_COMPOSE_FILE) run --build --rm artifact-custody-migrate
	docker compose --env-file .env.artifact-custody -f $(BC06_COMPOSE_FILE) up --build --wait -d artifact-custody

artifact-custody-status:
	docker compose --env-file .env.artifact-custody -f $(BC06_COMPOSE_FILE) ps

artifact-custody-down:
	docker compose --env-file .env.artifact-custody -f $(BC06_COMPOSE_FILE) down

artifact-custody-integration: artifact-custody-up
	@test -f $(MONGO_ENV_FILE) || { echo 'Run make acquisition-output-mongo-setup first or provide the preserved MongoDB worktree environment.'; exit 2; }
	@set -a; . ./.env.artifact-custody; . ./$(MONGO_ENV_FILE); set +a; ARTIFACT_CUSTODY_HTTP_URL=http://127.0.0.1:8086 uv run --locked --all-packages python tooling/artifact_custody_integration.py

artifact-custody-reconcile:
	docker compose --env-file .env.artifact-custody -f $(BC06_COMPOSE_FILE) --profile operations run --build --rm artifact-custody-reconcile

artifact-custody-recovery-tests: artifact-custody-up
	docker compose --env-file .env.artifact-custody -f $(BC06_COMPOSE_FILE) --profile tests run --build --rm artifact-custody-integration-tests

artifact-custody-benchmark: artifact-custody-up
	@set -a; . ./.env.artifact-custody; set +a; ARTIFACT_CUSTODY_HTTP_URL=http://127.0.0.1:8086 uv run --locked python tooling/benchmark_artifact_custody.py

artifact-custody-backup-restore: artifact-custody-up
	@set -a; . ./.env.artifact-custody; set +a; BC06_LOCAL_POSTGRES_PORT=$${SCOUT_POSTGRES_PORT:-55432} uv run --locked python tooling/artifact_custody_backup_restore_smoke.py

acquisition-output-fixture-example:
	uv run --locked --package mozaic-acquisition-output python examples/acquisition-output-fixture/consume.py

acquisition-output-mongo-setup:
	@if [ ! -f $(MONGO_ENV_FILE) ]; then uv run --locked python tooling/mongodb_local_setup.py; else echo 'Using the existing MongoDB local environment.'; fi

acquisition-output-mongo-check-config: acquisition-output-mongo-setup
	@test -f $(MONGO_ENV_FILE) || { echo 'Run make acquisition-output-mongo-setup first.'; exit 2; }
	@set -a; . ./$(MONGO_ENV_FILE); set +a; uv run --locked --all-packages python -m acquisition_output.adapters.mongodb.cli check-config

acquisition-output-mongo-up: acquisition-output-mongo-check-config
	docker compose --env-file $(MONGO_ENV_FILE) -f $(MONGO_COMPOSE_FILE) up --wait -d mongodb
	docker compose --env-file $(MONGO_ENV_FILE) -f $(MONGO_COMPOSE_FILE) run --rm mongodb-init

acquisition-output-mongo-status:
	docker compose --env-file $(MONGO_ENV_FILE) -f $(MONGO_COMPOSE_FILE) ps

acquisition-output-mongo-connectivity: acquisition-output-mongo-up
	@set -a; . ./$(MONGO_ENV_FILE); set +a; uv run --locked --all-packages python -m acquisition_output.adapters.mongodb.cli ping
	docker compose --env-file $(MONGO_ENV_FILE) -f $(MONGO_COMPOSE_FILE) exec mongodb bash -ec 'mongosh --quiet --host mongodb:27017 --username "$$MONGO_INITDB_ROOT_USERNAME" --password "$$MONGO_INITDB_ROOT_PASSWORD" --authenticationDatabase admin --eval '\''db.hello().setName'\'''

acquisition-output-mongo-provision: acquisition-output-mongo-up
	@set -a; . ./$(MONGO_ENV_FILE); set +a; uv run --locked --all-packages python -m acquisition_output.adapters.mongodb.cli provision

acquisition-output-mongo-tests: acquisition-output-mongo-provision
	@set -a; . ./$(MONGO_ENV_FILE); set +a; uv run --locked --all-packages python -m unittest discover -s apps/acquisition-output/integration_tests -v

acquisition-output-mongo-example: acquisition-output-mongo-provision
	@set -a; . ./$(MONGO_ENV_FILE); set +a; uv run --locked --all-packages python examples/acquisition-output-mongodb/consume.py

acquisition-output-mongo-backup-restore: acquisition-output-mongo-provision
	@set -a; . ./$(MONGO_ENV_FILE); set +a; uv run --locked --all-packages python tooling/mongodb_backup_restore_smoke.py

acquisition-output-mongo-benchmark: acquisition-output-mongo-provision
	@set -a; . ./$(MONGO_ENV_FILE); set +a; uv run --locked --all-packages python tooling/benchmark_acquisition_output_mongodb.py

acquisition-output-mongo-benchmark-100k: acquisition-output-mongo-provision
	@set -a; . ./$(MONGO_ENV_FILE); set +a; uv run --locked --all-packages python tooling/benchmark_acquisition_output_mongodb.py --counts=100000

acquisition-output-mongo-down:
	docker compose --env-file $(MONGO_ENV_FILE) -f $(MONGO_COMPOSE_FILE) down

execution-coordination-protected-e2e:
	uv run --locked --all-packages python tooling/run_execution_coordination_e2e.py --docker

test:
	uv run --locked python -m unittest discover -s tests -v

check: lock-check lint format-check docs-check migration-check naming-check architecture-check artifact-custody-go-format-check artifact-custody-go-vet artifact-custody-go-test acquisition-gateway-go-format-check acquisition-gateway-go-vet acquisition-gateway-go-test test-libraries test-scrapy-collector collector-live-admission-tests execution-coordination-tests acquisition-output-tests acquisition-application-tests test

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
