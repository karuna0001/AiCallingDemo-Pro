#!/bin/sh
# POSIX sh so this works on slim Alpine/Debian base images without bash.
set -eu
cd "$(dirname "$0")"

echo "🚀 Starting OutboundAI..."

# VPS / container env vars are the single source of truth.
# .env is used ONLY for local development, and only for variables the host has not already set.
if [ -f ".env" ]; then
    echo "   Found local .env — loading ONLY unset variables (VPS env always wins)"
    # shellcheck disable=SC2046
    set -a
    while IFS= read -r line || [ -n "$line" ]; do
        case "$line" in
            ''|\#*) continue ;;
        esac
        key="${line%%=*}"
        # Only export if currently unset
        if [ -z "$(printenv "$key" || true)" ]; then
            export "$line"
        fi
    done < .env
    set +a
fi

echo "📋 Configuration:"
echo "   LiveKit:  ${LIVEKIT_URL:-<missing>}"
echo "   Gemini:   ${GEMINI_MODEL:-gemini-3.1-flash-live-preview}"
echo "   Supabase: ${SUPABASE_URL:-<missing>}"

SERVER_PID=""
AGENT_PID=""

echo "🌐 Starting FastAPI on port ${PORT:-8000}..."
uvicorn server:app --host 0.0.0.0 --port "${PORT:-8000}" &
SERVER_PID=$!

# Propagate shutdown signals to both the web server and the agent worker
trap 'kill -TERM "$SERVER_PID" 2>/dev/null || true; kill -TERM "$AGENT_PID" 2>/dev/null || true; wait' INT TERM

sleep 2

echo "🤖 Starting LiveKit agent worker..."
python agent.py start &
AGENT_PID=$!

# Portable watchdog: exit as soon as either child dies so the
# container orchestrator (Coolify / Docker) can restart us.
while kill -0 "$SERVER_PID" 2>/dev/null && kill -0 "$AGENT_PID" 2>/dev/null; do
    sleep 2
done

# Reap whichever child is still alive
kill -TERM "$SERVER_PID" 2>/dev/null || true
kill -TERM "$AGENT_PID" 2>/dev/null || true
wait "$SERVER_PID" 2>/dev/null || true
wait "$AGENT_PID"  2>/dev/null || true

echo "❌ One of the processes exited — stopping container so the orchestrator can restart it."
exit 1
