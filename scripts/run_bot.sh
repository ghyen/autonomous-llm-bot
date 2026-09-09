#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

# Resolve python binary
if [ -n "${PYTHON_BIN:-}" ] && [ -x "$PYTHON_BIN" ]; then
    RESOLVED_PYTHON="$PYTHON_BIN"
elif [ -x "$PROJECT_ROOT/.venv/bin/python" ]; then
    RESOLVED_PYTHON="$PROJECT_ROOT/.venv/bin/python"
elif [ -x "/Users/edwin/.hermes/hermes-agent/venv/bin/python" ]; then
    RESOLVED_PYTHON="/Users/edwin/.hermes/hermes-agent/venv/bin/python"
elif command -v python3 >/dev/null 2>&1; then
    RESOLVED_PYTHON="$(command -v python3)"
else
    echo "❌ Error: No python interpreter found." >&2
    exit 1
fi

echo "🤖 Starting autonomous-llm-bot..."
echo "   Python:   $RESOLVED_PYTHON"
echo "   Workdir:  $PROJECT_ROOT"

cd "$PROJECT_ROOT"
exec "$RESOLVED_PYTHON" -u bot.py "$@"
