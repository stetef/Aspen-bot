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

# ---------------------------------------------------------------------------
# Optional: bootstrap `socat` for the Bash OS-sandbox (ASPEN_SANDBOX_ENABLED=true).
# Claude Code's sandbox needs `socat` (network relay) plus `bubblewrap`. Without
# sudo, this fetches the official AlmaLinux 8 socat RPM (ABI-compatible with
# RHEL 8) and extracts just the binary into ~/.local/bin — no root required.
# `bubblewrap` must already be installed by the system.
#
# Verified enforcing 2026-06-24 (Claude Code 2.1.190 + bubblewrap 0.4.0): writes
# outside the allow-list are blocked. The sandbox is disabled only when the bot is
# launched *nested* inside another Claude Code session — start.sh runs it as a
# normal top-level process, so that's fine. Verify anytime: ./verify_sandbox.sh
# from a plain shell.
# ---------------------------------------------------------------------------
ensure_socat() {
    command -v socat >/dev/null 2>&1 && { echo "socat already present: $(command -v socat)"; return 0; }
    echo "socat not found; fetching AlmaLinux 8 RPM into ~/.local/bin (no sudo) ..."
    local rpm="socat-1.7.4.1-2.el8_10.x86_64.rpm"
    local url="https://repo.almalinux.org/almalinux/8/AppStream/x86_64/os/Packages/$rpm"
    local tmp; tmp="$(mktemp -d)"
    if curl -fsSL "$url" -o "$tmp/$rpm" \
        && ( cd "$tmp" && rpm2cpio "$rpm" | cpio -idm --quiet ) \
        && mkdir -p "$HOME/.local/bin" \
        && cp "$tmp/usr/bin/socat" "$HOME/.local/bin/socat" \
        && chmod +x "$HOME/.local/bin/socat"; then
        echo "Installed socat -> $HOME/.local/bin/socat ($("$HOME/.local/bin/socat" -V | head -1))"
    else
        echo "WARNING: socat bootstrap failed; the sandbox will refuse to start if enabled."
    fi
    rm -rf "$tmp"
}

if grep -qiE '^[[:space:]]*ASPEN_SANDBOX_ENABLED[[:space:]]*=[[:space:]]*(1|true|yes)' .env; then
    export PATH="$HOME/.local/bin:$PATH"
    ensure_socat || true
fi

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
