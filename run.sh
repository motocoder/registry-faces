#!/usr/bin/env bash
# Launch the registry-faces desktop UI.
# Anchored to this script's directory so it works regardless of CWD.

set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" &> /dev/null && pwd)"
cd "$SCRIPT_DIR"

if [ ! -x ".venv/bin/registry-faces" ]; then
    echo "registry-faces not found in .venv. Set up the project first:" >&2
    echo "  python3 -m venv .venv" >&2
    echo "  .venv/bin/pip install -e '.[all]'" >&2
    exit 1
fi

exec .venv/bin/registry-faces --registry "$SCRIPT_DIR/registry" ui "$@"
