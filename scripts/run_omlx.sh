#!/usr/bin/env bash
set -euo pipefail

# Configuration with environment variable overrides
MODEL_DIR="${MODEL_DIR:-/Users/edwin/qwen38-mlx/models}"
HOST="${HOST:-127.0.0.1}"
PORT="${PORT:-18080}"
OMLX_BIN="${OMLX_BIN:-$(command -v omlx || echo "${HOME}/.local/bin/omlx")}"

if [ ! -x "$OMLX_BIN" ]; then
    echo "❌ Error: omlx binary not found at '$OMLX_BIN'." >&2
    echo "   Please install omlx (e.g. uv tool install omlx) or set OMLX_BIN." >&2
    exit 1
fi

if [ ! -d "$MODEL_DIR" ]; then
    echo "⚠️ Warning: Model directory '$MODEL_DIR' does not exist." >&2
fi

echo "🚀 Starting omlx serve..."
echo "   Model Dir: $MODEL_DIR"
echo "   Endpoint:  http://${HOST}:${PORT}"

exec "$OMLX_BIN" serve \
    --model-dir "$MODEL_DIR" \
    --host "$HOST" \
    --port "$PORT" \
    --log-level info \
    --memory-guard balanced \
    --max-concurrent-requests 1 \
    "$@"
