---
name: repository-checks
description: Use when preparing a Data Mozaic change for local validation or pull request review.
---

# Repository checks

1. Inspect `git status --short --branch` and preserve existing work.
2. Use `uv` for Python dependencies and Make targets as the local/CI interface.
3. `make check` and `make ci` are offline repository checks and must not require Docker.
4. PostgreSQL integration and recovery checks are optional local checks. They require Docker and a reviewed `.env` copied from `.env.example`; `.env` is local-only and must never be committed.
5. Report the exact commands and outcomes. Distinguish local test doubles from production integrations and never imply that a green CI approves Proposed ADRs.
