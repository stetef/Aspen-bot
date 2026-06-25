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
# Cap on a single file the agent may attach to a reply via the attach_file tool.
MAX_ATTACHMENT_BYTES  = int(os.getenv("MAX_ATTACHMENT_BYTES", str(25 * 1024 * 1024)))

# Tool server (only needed when run_python_analysis is used)
AGENT_INTERNAL_SECRET = os.getenv("AGENT_INTERNAL_SECRET", "")
TOOL_SERVER_URL       = os.getenv("TOOL_SERVER_URL", "http://127.0.0.1:27195")
WORKSPACE_ROOT        = Path(os.getenv("WORKSPACE_ROOT", "/aspen_workspace")).resolve()
FIGURE_ARCHIVE_DIR    = WORKSPACE_ROOT / "figure_archive"

# ---------------------------------------------------------------------------
# Claude Agent SDK backend
# ---------------------------------------------------------------------------
# Per-turn tool-call (agentic round) cap, passed to the SDK as max_turns.
AGENT_MAX_ROUNDS      = int(os.getenv("AGENT_MAX_ROUNDS", "25"))
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

# Built-in Bash tool allowlist (HPC job investigation: squeue, sacct, ...).
# The SDK backend exposes Claude Code's *built-in* Bash tool, but only for the
# command patterns listed here. Entries are Claude Code permission rules — the
# "Bash(cmd:*)" form is a prefix match. The CLI's bash parser checks every
# sub-command of a pipeline and refuses to auto-approve command substitution, so
# a pipeline needs every command in it allowlisted and "squeue $(...)" never
# auto-approves — matching commands run without prompting and everything else is
# denied by the can_use_tool lockdown in agent.py.
#
# SECURITY: the default is Slurm-ONLY. General text utilities (cat/head/tail/ls/
# grep/wc/sort/uniq) are deliberately NOT in the default. With the OS Bash sandbox
# off (SANDBOX_ENABLED=false, the default) the Bash tool runs as the bot's own
# Unix user with no path restriction, so any allowlisted user could have the agent
# read ANY file that user can — SSH private keys, this repo's .env (Slack tokens +
# AGENT_INTERNAL_SECRET), ~/.claude credentials. Calculations-root files stay
# available through the path-scoped read_file tool instead. Excluded for the same
# can't-see-the-flags reason: find (-exec/-delete), awk (system()), sed (w/e).
# Only widen this if you enable the OS sandbox with denyRead on the secret paths
# (ASPEN_SANDBOX_DENY_READ_PATHS), or you accept those commands running unconfined
# as the bot user.
_DEFAULT_BASH_ALLOWLIST = [
    "Bash(squeue:*)",         # job queue
    "Bash(sacct:*)",          # job accounting / history
    "Bash(sinfo:*)",          # partition / node info
    "Bash(sstat:*)",          # running-job stats
    "Bash(sprio:*)",          # job priorities
    "Bash(scontrol show:*)",  # read-only job/node detail (not bare scontrol)
]
BASH_ALLOWLIST = [
    p.strip()
    for p in os.getenv("ASPEN_BASH_ALLOWLIST", ",".join(_DEFAULT_BASH_ALLOWLIST)).split(",")
    if p.strip()
]


# ---------------------------------------------------------------------------
# Bash OS-level sandbox (Claude Code sandbox: bubblewrap on Linux, Seatbelt on
# macOS). When enabled, the agent's Bash commands run inside an OS sandbox whose
# read/write/network boundary is defined HERE by the operator — independent of
# the Unix user the bot runs as. This is how the agent gets *write* access to a
# controlled area without the bot's own account granting it everywhere.
#
# On Linux this needs `bubblewrap` and `socat` installed (apt/dnf). See
# https://code.claude.com/docs/en/sandboxing.
#
# Design (how this composes with BASH_ALLOWLIST above):
#   - Read-only investigation commands (squeue/sacct/...) are EXCLUDED from the
#     sandbox: Slurm clients need cluster network + the munge socket, which the
#     bwrap network jail blocks. Excluded commands run as the bot user but are
#     still auto-approved by BASH_ALLOWLIST (and anything off it is denied by the
#     can_use_tool backstop), so excluding them does not widen access.
#   - Every other command runs INSIDE the jail, auto-approved by the sandbox
#     boundary (autoAllow), able to write only within SANDBOX_WRITE_PATHS.
# ---------------------------------------------------------------------------
def _csv_env(name: str, default: str = "") -> list[str]:
    return [p.strip() for p in os.getenv(name, default).split(",") if p.strip()]


def _flag(name: str, default: str) -> bool:
    return os.getenv(name, default).lower() in ("1", "true", "yes")

# Verified enforcing 2026-06-24 (CLI 2.1.190 + bubblewrap 0.4.0) when the bot runs
# as a normal top-level process. Gotcha: Claude Code disables its Bash sandbox if
# launched *nested* inside another Claude Code session — so don't start the bot
# from within one. Re-check anytime with ./verify_sandbox.sh from a plain shell.
SANDBOX_ENABLED = _flag("ASPEN_SANDBOX_ENABLED", "false")
# Fail closed: if the sandbox can't start (missing bwrap, unsupported platform),
# refuse to run rather than silently dropping to UNsandboxed execution.
SANDBOX_FAIL_IF_UNAVAILABLE = _flag("ASPEN_SANDBOX_FAIL_IF_UNAVAILABLE", "true")
# Auto-approve Bash commands that successfully run inside the sandbox (the point
# of the jail — the OS boundary contains them, so no per-command prompt).
SANDBOX_AUTO_ALLOW = _flag("ASPEN_SANDBOX_AUTO_ALLOW", "true")
# Allow commands to escape the jail via dangerouslyDisableSandbox. Default false
# (strict): a command either runs sandboxed or is in SANDBOX_EXCLUDED_COMMANDS.
SANDBOX_ALLOW_UNSANDBOXED = _flag("ASPEN_SANDBOX_ALLOW_UNSANDBOXED", "false")
# Session working directory when sandboxed (also writable by default). Keep the
# agent out of the repo/home — point this at a scratch/workspace dir.
SANDBOX_WORKDIR = os.getenv("ASPEN_SANDBOX_WORKDIR", str(WORKSPACE_ROOT))
# Paths the sandboxed agent may WRITE to (beyond cwd + the session temp dir).
# This is the agent's writable surface — operator-controlled, separate from what
# the bot's Unix user could otherwise touch. Prefix rules: "/abs", "~/home", "rel".
SANDBOX_WRITE_PATHS = _csv_env("ASPEN_SANDBOX_WRITE_PATHS")
# Paths to deny reads of inside the jail (e.g. credentials). Empty = read-most.
SANDBOX_DENY_READ_PATHS = _csv_env("ASPEN_SANDBOX_DENY_READ_PATHS")
# Re-allow reads of specific paths inside a denied region.
SANDBOX_ALLOW_READ_PATHS = _csv_env("ASPEN_SANDBOX_ALLOW_READ_PATHS")
# Network domains the sandbox may reach. Empty = no network (safest); a command
# needing an unlisted domain fails rather than hanging on a prompt.
SANDBOX_ALLOWED_DOMAINS = _csv_env("ASPEN_SANDBOX_ALLOWED_DOMAINS")
# Unix socket paths reachable inside the jail (e.g. an SSH agent). Be careful —
# some sockets (docker.sock) are a sandbox escape.
SANDBOX_UNIX_SOCKETS = _csv_env("ASPEN_SANDBOX_UNIX_SOCKETS")
# Commands that run OUTSIDE the jail. Default = the read-only Slurm clients,
# which need cluster network/munge the jail blocks (still gated by BASH_ALLOWLIST).
SANDBOX_EXCLUDED_COMMANDS = _csv_env(
    "ASPEN_SANDBOX_EXCLUDED_COMMANDS",
    "squeue,sacct,sinfo,sstat,sprio,scontrol",
)
