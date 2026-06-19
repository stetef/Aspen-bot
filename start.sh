#!/usr/bin/env bash
# start.sh — launch both Aspen processes for dev mode
# Run from the aspen-bot/ directory in a screen session.
#
# Usage:
#   screen -S aspen
#   cd /path/to/aspen-bot
#   bash start.sh
#
# Ctrl+A, D to detach.  screen -r aspen to re-attach.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

if [[ ! -f .env ]]; then
    echo "ERROR: .env not found. Copy .env.example to .env and fill in your tokens."
    exit 1
fi

source venv/bin/activate

# Start tool server in the background
echo "Starting tool server on 127.0.0.1:8000 ..."
python tool_server.py &
TOOL_SERVER_PID=$!

# Give it a moment to bind
sleep 2

# Verify it's up
if ! kill -0 "$TOOL_SERVER_PID" 2>/dev/null; then
    echo "ERROR: tool server failed to start. Check the output above."
    exit 1
fi

echo "Tool server running (PID $TOOL_SERVER_PID)"
echo "Starting Slack bot ..."

# Run bot in foreground so Ctrl+C or screen window close stops both
trap "echo 'Stopping...'; kill $TOOL_SERVER_PID 2>/dev/null; exit 0" INT TERM

python aspen-bot.py

# If bot exits, clean up tool server too
kill "$TOOL_SERVER_PID" 2>/dev/null || true
