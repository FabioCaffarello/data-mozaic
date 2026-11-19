#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd -- "$(dirname -- "$0")" && pwd -P)"
repo_root="$(cd -- "$script_dir/.." && pwd -P)"
environment_file="$repo_root/.local/scout-mcp.env"

if [[ ! -f "$environment_file" ]]; then
  echo "Missing $environment_file; copy config/scout/authoring-mcp.env.example and configure an approved local source first." >&2
  exit 2
fi

exec uv run \
  --locked \
  --offline \
  --project "$repo_root" \
  --env-file "$environment_file" \
  --package mozaic-scout \
  python -m scout.tool_host
