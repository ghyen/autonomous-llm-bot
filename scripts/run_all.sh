#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HOST="${HOST:-127.0.0.1}"
PORT="${PORT:-18080}"
HEALTH_URL="http://${HOST}:${PORT}/v1/models"
TIMEOUT_SECS="${WAIT_TIMEOUT_SECS:-120}"

RAPID_PID=""

cleanup() {
    echo ""
    echo "🛑 Shutting down services..."
    if [ -n "$RAPID_PID" ] && kill -0 "$RAPID_PID" 2>/dev/null; then
        echo "   Stopping rapid-mlx (PID $RAPID_PID)..."
        kill -TERM "$RAPID_PID" 2>/dev/null || true
        wait "$RAPID_PID" 2>/dev/null || true
    fi
    echo "✅ All services stopped."
}

trap cleanup EXIT INT TERM

# Check if rapid-mlx is already running
if curl -s -f "$HEALTH_URL" >/dev/null 2>&1; then
    echo "ℹ️ rapid-mlx is already active at $HEALTH_URL. Using existing instance."
else
    echo "🚀 [1/2] Launching rapid-mlx server in background..."
    "$SCRIPT_DIR/run_rapid.sh" &
    RAPID_PID=$!

    echo "⏳ Waiting for rapid-mlx to become ready at $HEALTH_URL (timeout: ${TIMEOUT_SECS}s)..."
    start_time=$(date +%s)

    while true; do
        if curl -s -f "$HEALTH_URL" >/dev/null 2>&1; then
            break
        fi

        if ! kill -0 "$RAPID_PID" 2>/dev/null; then
            echo "❌ Error: rapid-mlx server exited unexpectedly." >&2
            exit 1
        fi

        current_time=$(date +%s)
        elapsed=$((current_time - start_time))
        if [ "$elapsed" -ge "$TIMEOUT_SECS" ]; then
            echo "❌ Error: Timed out waiting for rapid-mlx (${TIMEOUT_SECS}s)." >&2
            exit 1
        fi

        sleep 2
    done
    echo "✅ rapid-mlx is healthy and ready!"
fi

echo "🤖 [2/2] Launching autonomous-llm-bot..."
"$SCRIPT_DIR/run_bot.sh" "$@"
