#!/usr/bin/env python3
"""
Aspen — HPC Slack Agent
Phase 1: Read-only file inspection via Anthropic tool use, Slack Socket Mode.
"""

import os
import re
import time
import logging
from pathlib import Path
from threading import Lock, Semaphore
from collections import defaultdict
from typing import Optional

from dotenv import load_dotenv
import anthropic
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("aspen")

# ---------------------------------------------------------------------------
# Configuration — all values from .env, nothing hardcoded
# ---------------------------------------------------------------------------
SLACK_BOT_TOKEN     = os.environ["SLACK_BOT_TOKEN"]
SLACK_APP_TOKEN     = os.environ["SLACK_APP_TOKEN"]
ANTHROPIC_API_KEY   = os.environ["ANTHROPIC_API_KEY"]
CALCULATIONS_ROOT   = Path(os.environ["CALCULATIONS_ROOT"]).resolve()
ALLOWED_USER_IDS    = set(os.environ["ASPEN_ALLOWED_SLACK_USER_IDS"].split(","))
MODEL               = os.getenv("ANTHROPIC_MODEL", "claude-opus-4-5")

RATE_LIMIT_REQUESTS = int(os.getenv("RATE_LIMIT_REQUESTS", "5"))
RATE_LIMIT_WINDOW   = int(os.getenv("RATE_LIMIT_WINDOW_SECONDS", "600"))
CONTEXT_MAX_TURNS   = int(os.getenv("CONTEXT_MAX_TURNS", "20"))
CONTEXT_EXPIRY      = int(os.getenv("CONTEXT_EXPIRY_SECONDS", "14400"))
MAX_CONCURRENT      = int(os.getenv("MAX_CONCURRENT_EXECUTIONS", "2"))
MAX_FILE_BYTES      = int(os.getenv("MAX_FILE_READ_BYTES", "50000"))

anthropic_client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
app = App(token=SLACK_BOT_TOKEN)

# ---------------------------------------------------------------------------
# In-memory state
# ---------------------------------------------------------------------------
_history_lock = Lock()
_histories: dict[str, dict] = {}       # key → {"turns": [...], "last_ts": float}

_rate_lock   = Lock()
_rate_data:   dict[str, list[float]] = defaultdict(list)   # uid → [timestamps]
_user_active: dict[str, bool]        = defaultdict(bool)   # uid → in-flight?

_global_sem = Semaphore(MAX_CONCURRENT)

# ---------------------------------------------------------------------------
# System prompt
# ---------------------------------------------------------------------------
SYSTEM_PROMPT = (
    "You are Aspen, a research assistant for an HPC computational chemistry group. "
    "You have read-only access to a calculations directory and help scientists "
    "understand their results, input files, and job outputs.\n\n"
    "Use list_directory to explore the directory tree, then read_file to inspect "
    "specific files. Be concise and scientific. When reading output files, focus "
    "on key results (energies, convergence, structures) rather than reproducing "
    "raw content verbatim.\n\n"
    f"Calculations root: {CALCULATIONS_ROOT}\n\n"
    "You cannot write, modify, or delete any files."
)

# ---------------------------------------------------------------------------
# Read-only file tools
# ---------------------------------------------------------------------------
TOOLS = [
    {
        "name": "list_directory",
        "description": "List contents of a directory under the calculations root.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": (
                        "Path relative to the calculations root "
                        "(e.g. 'thermolysin/ca-fixed'). Use '.' for the root."
                    ),
                }
            },
            "required": ["path"],
        },
    },
    {
        "name": "read_file",
        "description": "Read the text contents of a file under the calculations root.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Path relative to the calculations root.",
                }
            },
            "required": ["path"],
        },
    },
]


def _safe_path(rel: str) -> Optional[Path]:
    """
    Resolve a relative path against CALCULATIONS_ROOT and confirm it does not
    escape via symlinks or '..' traversal. Returns None if the path is unsafe.
    """
    try:
        resolved = (CALCULATIONS_ROOT / rel).resolve()
        resolved.relative_to(CALCULATIONS_ROOT)  # raises ValueError if outside
        return resolved
    except (ValueError, OSError):
        return None


def _list_directory(rel: str) -> str:
    path = _safe_path(rel)
    if path is None:
        return f"Error: '{rel}' is outside the allowed directory."
    if not path.exists():
        return f"Error: '{rel}' does not exist."
    if not path.is_dir():
        return f"Error: '{rel}' is not a directory."
    try:
        entries = sorted(path.iterdir(), key=lambda e: (e.is_file(), e.name))
        lines = [f"{'[dir]' if e.is_dir() else '[file]'} {e.name}" for e in entries]
        header = f"Contents of '{rel}' ({len(entries)} entries):"
        return header + "\n" + "\n".join(lines) if lines else f"'{rel}' is empty."
    except PermissionError:
        return f"Error: permission denied for '{rel}'."


def _read_file(rel: str) -> str:
    path = _safe_path(rel)
    if path is None:
        return f"Error: '{rel}' is outside the allowed directory."
    if not path.exists():
        return f"Error: '{rel}' does not exist."
    if not path.is_file():
        return f"Error: '{rel}' is not a regular file."
    try:
        size = path.stat().st_size
        with open(path, "r", errors="replace") as fh:
            content = fh.read(MAX_FILE_BYTES)
        truncation_note = (
            f"\n[Truncated: showing first {MAX_FILE_BYTES} of {size} bytes]"
            if size > MAX_FILE_BYTES else ""
        )
        return f"--- {rel} ---\n{content}{truncation_note}"
    except PermissionError:
        return f"Error: permission denied for '{rel}'."


TOOL_FNS = {
    "list_directory": lambda inp: _list_directory(inp["path"]),
    "read_file":      lambda inp: _read_file(inp["path"]),
}

# ---------------------------------------------------------------------------
# Conversation history helpers
# ---------------------------------------------------------------------------
def _thread_key(event: dict) -> str:
    ts = event.get("thread_ts") or event.get("ts", "")
    return f"{event.get('channel', '')}:{ts}"


def _get_history(key: str) -> list[dict]:
    with _history_lock:
        entry = _histories.get(key)
        if not entry:
            return []
        if time.time() - entry["last_ts"] > CONTEXT_EXPIRY:
            del _histories[key]
            return []
        return list(entry["turns"])


def _append_history(key: str, user_msg: str, assistant_msg: str) -> None:
    with _history_lock:
        entry = _histories.setdefault(key, {"turns": [], "last_ts": 0.0})
        entry["turns"].extend([
            {"role": "user",      "content": user_msg},
            {"role": "assistant", "content": assistant_msg},
        ])
        entry["turns"] = entry["turns"][-CONTEXT_MAX_TURNS:]
        entry["last_ts"] = time.time()

# ---------------------------------------------------------------------------
# Rate limiting helpers
# ---------------------------------------------------------------------------
def _check_rate_limit(uid: str) -> Optional[str]:
    """Return an error message if the user is rate-limited, else None."""
    now = time.time()
    with _rate_lock:
        ts_list = _rate_data[uid]
        ts_list[:] = [t for t in ts_list if now - t < RATE_LIMIT_WINDOW]
        if _user_active[uid]:
            return "I'm still working on your previous request. I'll post results here when it's done."
        if len(ts_list) >= RATE_LIMIT_REQUESTS:
            mins = RATE_LIMIT_WINDOW // 60
            return (
                f"You've sent {RATE_LIMIT_REQUESTS} requests in the last {mins} minutes. "
                "Please wait before asking again."
            )
        ts_list.append(now)
        _user_active[uid] = True
    return None


def _release_user(uid: str) -> None:
    with _rate_lock:
        _user_active[uid] = False

# ---------------------------------------------------------------------------
# Agentic loop
# ---------------------------------------------------------------------------
def _run_agent(user_message: str, history: list[dict]) -> str:
    """
    Calls Anthropic with tool-use enabled. Iterates until the model produces a
    final text response or the tool-call round limit is reached.
    """
    messages = history + [{"role": "user", "content": user_message}]

    for round_num in range(10):  # guard against runaway tool loops
        resp = anthropic_client.messages.create(
            model=MODEL,
            max_tokens=4096,
            system=SYSTEM_PROMPT,
            tools=TOOLS,
            messages=messages,
        )
        log.debug("Round %d: stop_reason=%s", round_num, resp.stop_reason)

        if resp.stop_reason == "end_turn":
            return "\n".join(
                b.text for b in resp.content if hasattr(b, "text")
            ) or "(no text response)"

        if resp.stop_reason == "tool_use":
            messages.append({"role": "assistant", "content": resp.content})
            tool_results = []
            for block in resp.content:
                if block.type != "tool_use":
                    continue
                fn = TOOL_FNS.get(block.name)
                result = fn(block.input) if fn else f"Unknown tool: {block.name}"
                log.info("Tool %-20s path=%s → %d chars", block.name, block.input, len(result))
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": result,
                })
            messages.append({"role": "user", "content": tool_results})
            continue

        # Unexpected stop reason (e.g. "max_tokens")
        log.warning("Unexpected stop_reason: %s", resp.stop_reason)
        break

    return "I wasn't able to complete your request within the tool-call limit. Please try a simpler query."

# ---------------------------------------------------------------------------
# Slack event handlers
# ---------------------------------------------------------------------------
def _handle_event(event: dict, say, strip_mention: bool) -> None:
    """Shared dispatch logic for both channel mentions and DMs."""
    uid       = event.get("user", "")
    thread_ts = event.get("thread_ts") or event.get("ts")

    # 1. Allowlist check — first gate
    if uid not in ALLOWED_USER_IDS:
        say(text="Sorry, you're not authorized to use Aspen.", thread_ts=thread_ts)
        return

    # 2. Per-user rate limit + concurrency check
    err = _check_rate_limit(uid)
    if err:
        say(text=err, thread_ts=thread_ts)
        return

    # 3. Global concurrency cap
    if not _global_sem.acquire(blocking=False):
        _release_user(uid)
        say(text="Aspen is busy right now — please try again in a moment.", thread_ts=thread_ts)
        return

    try:
        raw          = event.get("text", "")
        user_message = re.sub(r"<@[A-Z0-9]+>", "", raw).strip() if strip_mention else raw.strip()

        if not user_message:
            say(text="Hi! Ask me anything about the calculations.", thread_ts=thread_ts)
            return

        say(text="_Thinking…_", thread_ts=thread_ts)

        key     = _thread_key(event)
        history = _get_history(key)

        try:
            reply = _run_agent(user_message, history)
        except anthropic.APIError as exc:
            log.error("Anthropic API error for user %s: %s", uid, type(exc).__name__)
            reply = f"Sorry, there was an API error ({type(exc).__name__}). Please try again."
        except Exception:
            log.exception("Unexpected error for user %s", uid)
            reply = "Sorry, something went wrong on my end. Please try again."

        _append_history(key, user_message, reply)
        say(text=reply, thread_ts=thread_ts)

    finally:
        _release_user(uid)
        _global_sem.release()


@app.event("app_mention")
def handle_mention(event: dict, say) -> None:
    """Respond to @Aspen mentions in channels."""
    _handle_event(event, say, strip_mention=True)


@app.event("message")
def handle_dm(event: dict, say) -> None:
    """Respond to direct messages sent to the bot."""
    # Only handle DMs; ignore bot messages and message subtypes (edits, deletions, etc.)
    if event.get("channel_type") != "im":
        return
    if event.get("subtype") or event.get("bot_id"):
        return
    _handle_event(event, say, strip_mention=False)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    log.info("Starting Aspen  model=%s  calculations_root=%s", MODEL, CALCULATIONS_ROOT)
    SocketModeHandler(app, SLACK_APP_TOKEN).start()
