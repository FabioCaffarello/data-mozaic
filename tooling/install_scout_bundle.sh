#!/bin/sh
set -eu

if [ "$#" -ne 2 ]; then
  echo "usage: install_scout_bundle.sh BUNDLE_DIR PREFIX" >&2
  exit 2
fi
bundle=$(cd "$1" && pwd -P)
prefix=$2
if [ -e "$prefix" ]; then
  echo "destination must not already exist" >&2
  exit 2
fi
python3 "$bundle/verify_scout_bundle.py" "$bundle"
mkdir -p "$prefix"
uv venv --python 3.12 "$prefix/venv"
uv pip install --python "$prefix/venv/bin/python" --no-index \
  --find-links "$bundle/wheelhouse" --require-hashes \
  --requirement "$bundle/requirements.txt"
uv pip install --python "$prefix/venv/bin/python" --no-deps "$bundle"/wheels/*.whl
install -m 0644 "$bundle/manifest.json" "$prefix/manifest.json"
chmod -R go-w "$prefix"
echo "Installed unprivileged staging tree: $prefix"
