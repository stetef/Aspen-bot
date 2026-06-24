"""
Backend-agnostic tools: read-only file browsing + the sandboxed-analysis bridge.

``TOOL_SPECS`` is the single source of truth (name / description / input_schema /
impl). From it we derive ``TOOL_FNS`` (name → impl(input, context) -> (text,
figures)). ``dispatch()`` calls an impl, drains its figures into the per-turn sink
(``context["figures"]``) and returns the text only. The SDK backend wraps these
specs as ``@tool`` handlers; the figure-sink seam lives entirely in ``dispatch``.
"""

import logging
import os
from pathlib import Path
from typing import Optional

import requests

from . import config

log = logging.getLogger("aspen")


# --------------------------------------------------------------------------- #
# Read-only file tools
# --------------------------------------------------------------------------- #
def _safe_path(rel: str) -> Optional[Path]:
    """
    Resolve a relative path against CALCULATIONS_ROOT and confirm it does not
    escape via symlinks or '..' traversal. Returns None if the path is unsafe.
    """
    try:
        resolved = (config.CALCULATIONS_ROOT / rel).resolve()
        resolved.relative_to(config.CALCULATIONS_ROOT)  # raises ValueError if outside
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
            content = fh.read(config.MAX_FILE_BYTES)
        truncation_note = (
            f"\n[Truncated: showing first {config.MAX_FILE_BYTES} of {size} bytes]"
            if size > config.MAX_FILE_BYTES else ""
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
    if not config.AGENT_INTERNAL_SECRET:
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
            f"{config.TOOL_SERVER_URL}/run_python_analysis/{project_name}",
            json=payload,
            headers={"x-agent-secret": config.AGENT_INTERNAL_SECRET},
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


# --------------------------------------------------------------------------- #
# Tool specs — single source of truth for both backends
# --------------------------------------------------------------------------- #
TOOL_SPECS = [
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
        "impl": lambda inp, _ctx: (_list_directory(inp["path"]), []),
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
        "impl": lambda inp, _ctx: (_read_file(inp["path"]), []),
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
                        "Python code to execute. Import only libraries listed under "
                        "'Python libraries available for analysis' in the project's metadata.md. "
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
        "impl": _call_tool_server,
    },
]

# name → impl(input, context) -> (text, figures)
TOOL_FNS = {s["name"]: s["impl"] for s in TOOL_SPECS}


def dispatch(name: str, tool_input: dict, context: dict) -> str:
    """
    Run a tool by name, draining any figures into the per-turn sink
    (``context["figures"]``), and return just the tool-result text.
    """
    figures = context.setdefault("figures", [])
    fn = TOOL_FNS.get(name)
    if fn is None:
        return f"Unknown tool: {name}"
    result_text, figs = fn(tool_input, context)
    figures.extend(figs)
    return result_text
