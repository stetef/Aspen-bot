"""
Configuration — all values from .env, nothing hardcoded.

Other modules read these as ``config.<NAME>`` (at call time) so they stay
overridable and testable. Import-time behavior matches the original single file:
required env vars raise ``KeyError`` if missing, and the Anthropic client is built
here at import.
"""

import logging
import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("aspen")

# ---------------------------------------------------------------------------
# Required / core configuration (unchanged from the original)
# ---------------------------------------------------------------------------
SLACK_BOT_TOKEN     = os.environ["SLACK_BOT_TOKEN"]
SLACK_APP_TOKEN     = os.environ["SLACK_APP_TOKEN"]
# ANTHROPIC_API_KEY is not read here. The agent authenticates the Claude
# Code CLI (via the subscription login, or the key passed to the CLI subprocess
# when ASPEN_SDK_USE_SUBSCRIPTION=false) — see agent.py.
CALCULATIONS_ROOT     = Path(os.environ["CALCULATIONS_ROOT"]).resolve()
ALLOWED_USER_IDS      = set(os.environ["ASPEN_ALLOWED_SLACK_USER_IDS"].split(","))
MODEL                 = os.getenv("ANTHROPIC_MODEL", "claude-opus-4-8")

RATE_LIMIT_REQUESTS   = int(os.getenv("RATE_LIMIT_REQUESTS", "5"))
RATE_LIMIT_WINDOW     = int(os.getenv("RATE_LIMIT_WINDOW_SECONDS", "600"))
CONTEXT_EXPIRY        = int(os.getenv("CONTEXT_EXPIRY_SECONDS", "14400"))
MAX_CONCURRENT        = int(os.getenv("MAX_CONCURRENT_EXECUTIONS", "5"))
MAX_FILE_BYTES        = int(os.getenv("MAX_FILE_READ_BYTES", "50000"))

# Tool server (only needed when run_python_analysis is used)
AGENT_INTERNAL_SECRET = os.getenv("AGENT_INTERNAL_SECRET", "")
TOOL_SERVER_URL       = os.getenv("TOOL_SERVER_URL", "http://127.0.0.1:27195")
WORKSPACE_ROOT        = Path(os.getenv("WORKSPACE_ROOT", "/aspen_workspace")).resolve()
FIGURE_ARCHIVE_DIR    = WORKSPACE_ROOT / "figure_archive"

# ---------------------------------------------------------------------------
# Claude Agent SDK backend
# ---------------------------------------------------------------------------
# Per-turn tool-call (agentic round) cap, passed to the SDK as max_turns.
AGENT_MAX_ROUNDS      = int(os.getenv("AGENT_MAX_ROUNDS", "10"))
# Upper bound on concurrently parked conversation sessions (bounds warm SDK
# CLI subprocesses).
MAX_OPEN_SESSIONS     = int(os.getenv("MAX_OPEN_SESSIONS", "20"))
# Path to the Claude Code CLI binary. Empty = auto-discover "claude" on PATH;
# set it when PATH is minimal (e.g. under systemd).
CLAUDE_CLI_PATH       = os.getenv("CLAUDE_CLI_PATH", "")
# Auth: when true, the CLI uses the Claude Code login (subscription) by
# withholding ANTHROPIC_API_KEY from the CLI subprocess. Set false to let the CLI
# use ANTHROPIC_API_KEY (API billing) instead.
ASPEN_SDK_USE_SUBSCRIPTION = os.getenv("ASPEN_SDK_USE_SUBSCRIPTION", "true").lower() in ("1", "true", "yes")

# Built-in Bash tool allowlist (HPC job investigation: squeue, sacct, grep, ...).
# The SDK backend exposes Claude Code's *built-in* Bash tool, but only for the
# command patterns listed here. Entries are Claude Code permission rules — the
# "Bash(cmd:*)" form is a prefix match. The CLI's bash parser checks every
# sub-command of a pipeline and refuses to auto-approve command substitution, so
# "squeue | grep R" needs both squeue and grep allowlisted, and "squeue $(...)"
# never auto-approves — matching commands run without prompting and everything
# else is denied by the can_use_tool lockdown in agent.py.
#
# Defaults are read-only. Note: find (-exec/-delete), awk (system()), and sed
# (w/e) are intentionally EXCLUDED — their flags can write files or run arbitrary
# commands, which the prefix match cannot see. Only add such commands if you
# accept that they escape the read-only intent.
_DEFAULT_BASH_ALLOWLIST = [
    "Bash(squeue:*)",         # job queue
    "Bash(sacct:*)",          # job accounting / history
    "Bash(sinfo:*)",          # partition / node info
    "Bash(sstat:*)",          # running-job stats
    "Bash(sprio:*)",          # job priorities
    "Bash(scontrol show:*)",  # read-only job/node detail (not bare scontrol)
    "Bash(grep:*)",           # filter output (e.g. squeue | grep)
    "Bash(ls:*)",
    "Bash(cat:*)",
    "Bash(head:*)",
    "Bash(tail:*)",
    "Bash(wc:*)",
    "Bash(sort:*)",
    "Bash(uniq:*)",
]
BASH_ALLOWLIST = [
    p.strip()
    for p in os.getenv("ASPEN_BASH_ALLOWLIST", ",".join(_DEFAULT_BASH_ALLOWLIST)).split(",")
    if p.strip()
]
