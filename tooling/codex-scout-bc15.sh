#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd -- "$(dirname -- "$0")" && pwd -P)"
repo_root="$(cd -- "$script_dir/.." && pwd -P)"

if [[ ! -x "$repo_root/.venv/bin/python" ]]; then
  echo "Missing the synchronized workspace environment at $repo_root/.venv; run 'make sync' first." >&2
  exit 2
fi

export UV_CACHE_DIR="$repo_root/.local/uv-cache"

exec uv run \
  --locked \
  --offline \
  --no-sync \
  --project "$repo_root" \
  --package mozaic-collector-engineering \
  python -m collector_engineering "$@"
