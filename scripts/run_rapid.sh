#!/usr/bin/env bash
set -euo pipefail

# Configuration with environment variable overrides
MODEL_PATH="${MODEL_PATH:-/Users/edwin/qwen38-mlx/models/Qwen3.8-27B-Huihui-Abliterated-oQ4e-MTP-MLX}"
HOST="${HOST:-127.0.0.1}"
PORT="${PORT:-18080}"
RAPID_BIN="${RAPID_BIN:-$(command -v rapid-mlx || echo "/opt/homebrew/bin/rapid-mlx")}"

if [ ! -x "$RAPID_BIN" ]; then
    echo "❌ Error: rapid-mlx binary not found at '$RAPID_BIN'." >&2
    echo "   Please install rapid-mlx or set RAPID_BIN environment variable." >&2
    exit 1
fi

if [ ! -d "$MODEL_PATH" ]; then
    echo "⚠️ Warning: Model directory '$MODEL_PATH' does not exist." >&2
    echo "   Continuing anyway in case it's a Hugging Face repo ID..." >&2
fi

echo "🚀 Starting rapid-mlx serve..."
echo "   Model:    $MODEL_PATH"
echo "   Endpoint: http://${HOST}:${PORT}"

exec "$RAPID_BIN" serve "$MODEL_PATH" \
    --host "$HOST" \
    --port "$PORT" \
    --reasoning \
    --reasoning-parser qwen3 \
    --prefix-cache-index radix \
    --pin-system-prompt \
    --no-mllm \
    --hybrid-cache-entries 50 \
    --prefill-step-size 512 \
    --gpu-memory-utilization 0.85 \
    --kv-cache-dtype int8 \
    --max-tokens 16384 \
    --timeout 3600 \
    "$@"
