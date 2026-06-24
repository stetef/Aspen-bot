#!/usr/bin/env python3
"""
Aspen — HPC Slack Agent
Slack Socket Mode front-end. Calls tool_server.py for sandboxed analysis.
"""

import json
import os
import re
import shutil
import time
import logging
from pathlib import Path
from threading import Lock, Semaphore
from collections import defaultdict
from typing import Optional

import requests
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
    "You have read-only access to a calculations directory and can run sandboxed "
    "Python analysis code to help scientists understand results, plot data, and "
    "explore their calculations.\n\n"
    "To explore files: use list_directory and read_file.\n"
    "To analyze data: use run_python_analysis (runs in a secure sandbox).\n\n"
    f"Calculations root (for browsing): {CALCULATIONS_ROOT}\n"
    "Projects root (for analysis): set via PROJECTS_ROOT in .env\n\n"
    "When writing analysis code:\n"
    "- Save figures to /aspen_workspace/figures/ with plt.savefig(), default dpi=200\n"
    "- Print summary statistics rather than raw data\n"
    "- You cannot use subprocess, socket, or network operations\n"
    "- You cannot write, modify, or delete any files outside the workspace\n\n"
    "You cannot write, modify, or delete project files."
)

# ---------------------------------------------------------------------------
# Read-only file tools + sandboxed analysis tool
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
    {
        "name": "run_python_analysis",
        "description": (
            "Execute Python code in a secure sandbox to analyze project data. "
            "Use for plotting, statistics, or reading structured output files. "
            "The project directory is mounted read-only at /projects/<project_name>/. "
            "Save figures to /aspen_workspace/figures/ — they will be uploaded to Slack."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "project_name": {
                    "type": "string",
                    "description": "Name of the project directory under PROJECTS_ROOT.",
                },
                "code": {
                    "type": "string",
                    "description": (
                        "Python code to execute. Import only libraries in the project's "
                        "allowed_libraries (from metadata.toml/yaml). "
                        "Project data is at /projects/<project_name>/. "
                        "Save figures with plt.savefig('/aspen_workspace/figures/<name>.png', dpi=150)."
                    ),
                },
                "dataset": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "List of run directory names within the project to analyze.",
                },
                "question": {
                    "type": "string",
                    "description": "The user's original question (used for caching).",
                },
            },
            "required": ["project_name", "code", "dataset", "question"],
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


def _call_tool_server(inp: dict, context: dict) -> tuple[str, list[str]]:
    """
    POST to the FastAPI tool server for run_python_analysis.
    Returns (tool_result_text, figure_paths).
    context: {user_id, username, thread_ts}
    """
    if not AGENT_INTERNAL_SECRET:
        return ("Error: AGENT_INTERNAL_SECRET not configured — tool server unavailable.", [])

    project_name = inp.get("project_name", "")
    payload = {
        "code":      inp.get("code", ""),
        "dataset":   inp.get("dataset", []),
        "question":  inp.get("question", ""),
        "user_id":   context.get("user_id", ""),
        "username":  context.get("username", ""),
        "thread_ts": context.get("thread_ts", ""),
    }
    try:
        resp = requests.post(
            f"{TOOL_SERVER_URL}/run_python_analysis/{project_name}",
            json=payload,
            headers={"x-agent-secret": AGENT_INTERNAL_SECRET},
            timeout=int(os.getenv("EXECUTION_TIMEOUT_SECONDS", "120")) + 10,
        )
    except requests.exceptions.ConnectionError:
        return ("Error: tool server is not running. Start it with: python tool_server.py", [])
    except requests.exceptions.Timeout:
        return ("Error: tool server request timed out.", [])

    if resp.status_code == 403:
        return ("Error: tool server authentication failed.", [])
    if resp.status_code == 400:
        detail = resp.json().get("detail", resp.text)
        return (f"Error: {detail}", [])
    if resp.status_code == 422:
        detail = resp.json().get("detail", resp.text)
        return (f"Setup required: {detail}", [])
    if not resp.ok:
        return (f"Error: tool server returned HTTP {resp.status_code}.", [])

    data = resp.json()
    figures = data.get("figures", [])
    oversized = data.get("oversized_figures", [])

    lines = [f"Status: {data['status']}  ({data.get('duration_seconds', 0):.1f}s)"]
    if data.get("cache_hit"):
        lines[0] += "  [cached]"
    if data.get("stdout", "").strip():
        lines.append(f"\nOutput:\n{data['stdout']}")
    if data.get("stderr", "").strip():
        lines.append(f"\nStderr:\n{data['stderr']}")
    if data.get("truncated"):
        lines.append(
            "\n⚠️ Output was truncated (limit: 10,000 chars stdout / 2,000 chars stderr). "
            "Consider narrowing your dataset or printing only summary statistics."
        )
    if figures:
        lines.append(f"\nFigures generated: {len(figures)} file(s) — uploading to Slack.")
    if oversized:
        lines.append(
            f"\n{len(oversized)} figure(s) exceeded the 5 MB upload limit. "
            "Please regenerate at lower resolution (dpi=72, halved dimensions)."
        )
    if data["status"] == "timeout":
        lines.append(
            f"\nAnalysis timed out after {os.getenv('EXECUTION_TIMEOUT_SECONDS', 120)}s. "
            "Try a smaller dataset or a simpler query."
        )

    return ("\n".join(lines), figures)


TOOL_FNS = {
    "list_directory": lambda inp, _ctx: (_list_directory(inp["path"]), []),
    "read_file":      lambda inp, _ctx: (_read_file(inp["path"]), []),
    "run_python_analysis": _call_tool_server,
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
def _run_agent(
    user_message: str,
    history: list[dict],
    context: dict,
) -> tuple[str, list[str]]:
    """
    Calls Anthropic with tool-use enabled. Iterates until the model produces a
    final text response or the tool-call round limit is reached.
    Returns (reply_text, all_figures_collected).
    context: {user_id, username, thread_ts} — passed through to tool server.
    """
    messages = history + [{"role": "user", "content": user_message}]
    all_figures: list[str] = []

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
            text = "\n".join(
                b.text for b in resp.content if hasattr(b, "text")
            ) or "(no text response)"
            return text, all_figures

        if resp.stop_reason == "tool_use":
            messages.append({"role": "assistant", "content": resp.content})
            tool_results = []
            for block in resp.content:
                if block.type != "tool_use":
                    continue
                fn = TOOL_FNS.get(block.name)
                if fn is None:
                    result_text, figs = f"Unknown tool: {block.name}", []
                else:
                    result_text, figs = fn(block.input, context)
                all_figures.extend(figs)
                log.info("Tool %-22s → %d chars, %d figs", block.name, len(result_text), len(figs))
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": result_text,
                })
            messages.append({"role": "user", "content": tool_results})
            continue

        # Unexpected stop reason (e.g. "max_tokens")
        log.warning("Unexpected stop_reason: %s", resp.stop_reason)
        break

    return (
        "I wasn't able to complete your request within the tool-call limit. Please try a simpler query.",
        all_figures,
    )

# ---------------------------------------------------------------------------
# Slack event handlers
# ---------------------------------------------------------------------------
def _upload_figures(figures: list[str], client, channel: str, thread_ts: str) -> None:
    """Upload PNGs to Slack and move them to the figure archive."""
    for fig_path in figures:
        p = Path(fig_path)
        if not p.exists() or p.suffix.lower() != ".png":
            continue
        try:
            client.files_upload_v2(
                channel=channel,
                thread_ts=thread_ts,
                file=str(p),
                title=p.stem,
            )
            # Move to archive after successful upload
            FIGURE_ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
            shutil.move(str(p), str(FIGURE_ARCHIVE_DIR / p.name))
            log.info("Uploaded and archived figure: %s", p.name)
        except Exception:
            log.exception("Failed to upload figure %s", fig_path)


def _handle_event(event: dict, say, client, strip_mention: bool) -> None:
    """Shared dispatch logic for both channel mentions and DMs."""
    uid       = event.get("user", "")
    thread_ts = event.get("thread_ts") or event.get("ts")
    channel   = event.get("channel", "")

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
        context = {"user_id": uid, "username": "", "thread_ts": thread_ts or ""}

        try:
            reply, figures = _run_agent(user_message, history, context)
        except anthropic.APIError as exc:
            log.error("Anthropic API error for user %s: %s", uid, type(exc).__name__)
            reply, figures = f"Sorry, there was an API error ({type(exc).__name__}). Please try again.", []
        except Exception:
            log.exception("Unexpected error for user %s", uid)
            reply, figures = "Sorry, something went wrong on my end. Please try again.", []

        _append_history(key, user_message, reply)
        say(text=reply, thread_ts=thread_ts)

        if figures:
            _upload_figures(figures, client, channel, thread_ts)

    finally:
        _release_user(uid)
        _global_sem.release()


@app.event("app_mention")
def handle_mention(event: dict, say, client) -> None:
    """Respond to @Aspen mentions in channels."""
    _handle_event(event, say, client, strip_mention=True)


@app.event("message")
def handle_dm(event: dict, say, client) -> None:
    """Respond to direct messages sent to the bot."""
    # Only handle DMs; ignore bot messages and message subtypes (edits, deletions, etc.)
    if event.get("channel_type") != "im":
        return
    if event.get("subtype") or event.get("bot_id"):
        return
    _handle_event(event, say, client, strip_mention=False)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    log.info("Starting Aspen  model=%s  calculations_root=%s", MODEL, CALCULATIONS_ROOT)
    SocketModeHandler(app, SLACK_APP_TOKEN).start()
