.PHONY: help sync lock-check lint format-check docs-check test check hooks-install pre-commit ci

help:
	@printf '%s\n' 'sync          Install locked development dependencies' 'check         Run repository checks' 'hooks-install Install Git pre-commit hook' 'pre-commit    Run pre-commit on all tracked files' 'ci            Run the same checks used by CI'

sync:
	uv sync --locked

lock-check:
	uv lock --check

lint:
	uv run --locked ruff check tooling tests

format-check:
	uv run --locked ruff format --check tooling tests

docs-check:
	uv run --locked python tooling/check_docs.py

test:
	uv run --locked python -m unittest discover -s tests -v

check: lock-check lint format-check docs-check test

hooks-install: sync
	uv run --locked pre-commit install

pre-commit: sync
	uv run --locked pre-commit run --all-files

ci: sync check
