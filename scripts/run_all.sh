#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HOST="${HOST:-127.0.0.1}"
PORT="${PORT:-18080}"
HEALTH_URL="http://${HOST}:${PORT}/v1/models"
TIMEOUT_SECS="${WAIT_TIMEOUT_SECS:-120}"

SERVER_PID=""

cleanup() {
    echo ""
    echo "🛑 Shutting down services..."
    if [ -n "$SERVER_PID" ] && kill -0 "$SERVER_PID" 2>/dev/null; then
        echo "   Stopping LLM server (PID $SERVER_PID)..."
        kill -TERM "$SERVER_PID" 2>/dev/null || true
        wait "$SERVER_PID" 2>/dev/null || true
    fi
    echo "✅ All services stopped."
}

trap cleanup EXIT INT TERM

# Check if LLM server is already running
if curl -s -f "$HEALTH_URL" >/dev/null 2>&1; then
    echo "ℹ️ LLM server is already active at $HEALTH_URL. Using existing instance."
else
    echo "🚀 [1/2] Launching LLM server in background..."
    if [ -x "$SCRIPT_DIR/run_omlx.sh" ] && (command -v omlx >/dev/null 2>&1 || [ -x "${HOME}/.local/bin/omlx" ]); then
        "$SCRIPT_DIR/run_omlx.sh" &
    else
        "$SCRIPT_DIR/run_rapid.sh" &
    fi
    SERVER_PID=$!

    echo "⏳ Waiting for LLM server to become ready at $HEALTH_URL (timeout: ${TIMEOUT_SECS}s)..."
    start_time=$(date +%s)

    while true; do
        if curl -s -f "$HEALTH_URL" >/dev/null 2>&1; then
            break
        fi

        if ! kill -0 "$SERVER_PID" 2>/dev/null; then
            echo "❌ Error: LLM server exited unexpectedly." >&2
            exit 1
        fi

        current_time=$(date +%s)
        elapsed=$((current_time - start_time))
        if [ "$elapsed" -ge "$TIMEOUT_SECS" ]; then
            echo "❌ Error: Timed out waiting for LLM server (${TIMEOUT_SECS}s)." >&2
            exit 1
        fi

        sleep 2
    done
    echo "✅ LLM server is healthy and ready!"
fi

echo "🤖 [2/2] Launching autonomous-llm-bot..."
"$SCRIPT_DIR/run_bot.sh" "$@"
