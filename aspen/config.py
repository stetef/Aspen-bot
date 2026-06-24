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

import anthropic
from dotenv import load_dotenv

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("aspen")

# ---------------------------------------------------------------------------
# Required / core configuration (unchanged from the original)
# ---------------------------------------------------------------------------
SLACK_BOT_TOKEN     = os.environ["SLACK_BOT_TOKEN"]
SLACK_APP_TOKEN     = os.environ["SLACK_APP_TOKEN"]
ANTHROPIC_API_KEY   = os.environ["ANTHROPIC_API_KEY"]
CALCULATIONS_ROOT     = Path(os.environ["CALCULATIONS_ROOT"]).resolve()
ALLOWED_USER_IDS      = set(os.environ["ASPEN_ALLOWED_SLACK_USER_IDS"].split(","))
MODEL                 = os.getenv("ANTHROPIC_MODEL", "claude-opus-4-8")

RATE_LIMIT_REQUESTS   = int(os.getenv("RATE_LIMIT_REQUESTS", "5"))
RATE_LIMIT_WINDOW     = int(os.getenv("RATE_LIMIT_WINDOW_SECONDS", "600"))
CONTEXT_MAX_TURNS     = int(os.getenv("CONTEXT_MAX_TURNS", "20"))
CONTEXT_EXPIRY        = int(os.getenv("CONTEXT_EXPIRY_SECONDS", "14400"))
MAX_CONCURRENT        = int(os.getenv("MAX_CONCURRENT_EXECUTIONS", "2"))
MAX_FILE_BYTES        = int(os.getenv("MAX_FILE_READ_BYTES", "50000"))

# Tool server (only needed when run_python_analysis is used)
AGENT_INTERNAL_SECRET = os.getenv("AGENT_INTERNAL_SECRET", "")
TOOL_SERVER_URL       = os.getenv("TOOL_SERVER_URL", "http://127.0.0.1:8000")
WORKSPACE_ROOT        = Path(os.getenv("WORKSPACE_ROOT", "/aspen_workspace")).resolve()
FIGURE_ARCHIVE_DIR    = WORKSPACE_ROOT / "figure_archive"

# ---------------------------------------------------------------------------
# New for the backend-pluggable refactor (behavior-preserving defaults)
# ---------------------------------------------------------------------------
# Which agent backend to run. "messages" (default) reproduces today's behavior.
ASPEN_BACKEND         = os.getenv("ASPEN_BACKEND", "messages")
# Shared per-turn tool-call (agentic round) cap. Default 10 == the original
# hardcoded range(10); both backends use this one constant.
AGENT_MAX_ROUNDS      = int(os.getenv("AGENT_MAX_ROUNDS", "10"))
# Upper bound on concurrently parked conversation sessions (bounds warm SDK
# subprocesses in Phase 3; harmless for the Messages backend).
MAX_OPEN_SESSIONS     = int(os.getenv("MAX_OPEN_SESSIONS", "20"))
# Path to the Claude Code CLI binary (used only by the SDK backend). Empty =
# auto-discover "claude" on PATH; set it when PATH is minimal (e.g. systemd).
CLAUDE_CLI_PATH       = os.getenv("CLAUDE_CLI_PATH", "")

anthropic_client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
