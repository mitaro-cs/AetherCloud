#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV_PATH="$PROJECT_ROOT/.venv"
STORAGE_ROOT="${1:-$PROJECT_ROOT/data}"

python3 -m venv "$VENV_PATH"
"$VENV_PATH/bin/pip" install -r "$PROJECT_ROOT/requirements.txt"

mkdir -p "$STORAGE_ROOT/storage"

export AETHER_DB_PATH="$STORAGE_ROOT/users.db"
export AETHER_STORAGE_DIR="$STORAGE_ROOT/storage"
export AETHER_HOST="${AETHER_HOST:-0.0.0.0}"
export AETHER_PORT="${AETHER_PORT:-5000}"
export AETHER_DEBUG="${AETHER_DEBUG:-0}"

"$VENV_PATH/bin/python" "$PROJECT_ROOT/AetherCloud.py"
