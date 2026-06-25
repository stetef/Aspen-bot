#!/usr/bin/env bash
# verify_sandbox.sh
#
# Does Claude Code's built-in Bash OS-sandbox actually CONFINE writes in
# headless/SDK mode on THIS machine? (That's the mode Aspen runs in.)
#
# IMPORTANT: run this from a normal login shell — a fresh terminal or SSH
# session — NOT from inside a Claude Code session. Claude Code suppresses its
# Bash sandbox when it detects it's already nested inside another Claude session,
# which makes the result meaningless. The script refuses to run if it sees the
# nesting env vars.
#
# Usage:
#   bash verify_sandbox.sh
# Optional overrides:
#   CLAUDE_BIN=/path/to/claude  MODEL=claude-opus-4-8  bash verify_sandbox.sh

set -uo pipefail

CLAUDE="${CLAUDE_BIN:-claude}"
MODEL="${MODEL:-claude-opus-4-8}"
export PATH="$HOME/.local/bin:$PATH"   # so a ~/.local/bin/socat is found

# --- refuse to run nested inside a Claude Code session ---------------------
if [[ -n "${CLAUDECODE:-}${CLAUDE_CODE_ENTRYPOINT:-}${CLAUDE_CODE_CHILD_SESSION:-}${CLAUDE_CODE_SANDBOXED:-}" ]]; then
  echo "!! Detected a Claude Code session in the environment (CLAUDECODE / "
  echo "   CLAUDE_CODE_* is set). Nesting disables the sandbox and would give a"
  echo "   false result. Open a plain terminal or SSH session and run this there."
  exit 2
fi

command -v "$CLAUDE" >/dev/null || { echo "ERROR: '$CLAUDE' not on PATH"; exit 2; }

echo "=== environment ==="
echo "claude : $("$CLAUDE" --version 2>/dev/null)"
echo "bwrap  : $(command -v bwrap || echo MISSING) $(bwrap --version 2>/dev/null)"
echo "socat  : $(command -v socat || echo MISSING)"
echo

# --- set up an isolated test area ------------------------------------------
WORK="$(mktemp -d)"; mkdir -p "$WORK/run" "$WORK/allowed" "$WORK/forbidden"
trap 'rm -rf "$WORK"' EXIT
ALLOWED="$WORK/allowed/ok.txt"        # inside allowWrite  -> should succeed
FORBID="$WORK/forbidden/nope.txt"     # outside allowWrite -> should be BLOCKED
DBG="$WORK/debug.log"

# Sandbox config, inline JSON — exactly the shape the Agent SDK emits.
# allowWrite grants only $WORK/allowed; cwd ($WORK/run) is writable by default;
# $WORK/forbidden is neither, so a correct sandbox must block the write to it.
SJSON='{"sandbox":{"enabled":true,"autoAllowBashIfSandboxed":true,"allowUnsandboxedCommands":false,"failIfUnavailable":true,"network":{"allowedDomains":[]},"filesystem":{"allowWrite":["'"$WORK"'/allowed"]}}}'

PROMPT="Use the Bash tool to run EXACTLY this one command, then report its stdout verbatim:
echo data > $ALLOWED && echo ALLOWED_OK; echo data > $FORBID && echo FORBIDDEN_WROTE || echo FORBIDDEN_BLOCKED"

echo "=== running claude (headless) with sandbox enabled ==="
cd "$WORK/run"
timeout 300 "$CLAUDE" -p "$PROMPT" --settings "$SJSON" --model "$MODEL" \
  --debug-file "$DBG" < /dev/null
RC=$?
echo
echo "=== how the Bash command was executed (debug) ==="
grep -iE 'bwrap|sandbox|ro-bind|--bind|seccomp|Spawning shell|tool_dispatch' "$DBG" 2>/dev/null | head -15
echo

echo "================= VERDICT (ground truth on disk) ================="
echo "claude exit code        : $RC"
echo "bwrap mentions in debug : $(grep -ciE 'bwrap' "$DBG" 2>/dev/null)"
if [[ -f "$ALLOWED" ]]; then echo "allowed-path write      : SUCCEEDED (expected)"
else                          echo "allowed-path write      : did NOT happen (command may have been denied/not run)"; fi
if [[ -f "$FORBID" ]]; then
  echo "forbidden-path write    : SUCCEEDED"
  echo
  echo ">>> SANDBOX NOT ENFORCING: a write outside allowWrite went through."
  echo ">>> Do NOT rely on it as a security boundary on this setup."
  exit 1
else
  echo "forbidden-path write    : BLOCKED"
  echo
  echo ">>> SANDBOX ENFORCING: the write boundary held. The earlier nested-session"
  echo ">>> result was a test artifact; the feature works for the real bot process."
  exit 0
fi
