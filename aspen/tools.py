"""
Agent tools: read-only file browsing + the sandboxed-analysis bridge.

``TOOL_SPECS`` is the single source of truth (name / description / input_schema /
impl). From it we derive ``TOOL_FNS`` (name → impl(input, context) -> (text,
attachments)). ``dispatch()`` calls an impl, drains any attachment paths into the
per-turn sink (``context["attachments"]``) and returns the text only. Attachments
are any files to upload alongside the reply — plots from ``run_python_analysis``
and files the agent attaches via ``attach_file`` flow through the same sink. The
agent wraps these specs as ``@tool`` handlers; the sink seam lives entirely
in ``dispatch``.
"""

import logging
import os
import shutil
from datetime import datetime, timezone
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


def _attach_file(rel: str) -> tuple[str, list[str]]:
    """Mark a calculations-root file for upload alongside the reply.

    Returns (confirmation_or_error, [absolute_path]) — the path is drained into
    the per-turn attachment sink by ``dispatch`` and uploaded by the front-end.
    """
    path = _safe_path(rel)
    if path is None:
        return f"Error: '{rel}' is outside the allowed directory.", []
    if not path.exists():
        return f"Error: '{rel}' does not exist.", []
    if not path.is_file():
        return f"Error: '{rel}' is not a regular file.", []
    size = path.stat().st_size
    if size > config.MAX_ATTACHMENT_BYTES:
        return (
            f"Error: '{rel}' is {size / 1e6:.1f} MB, over the "
            f"{config.MAX_ATTACHMENT_BYTES / 1e6:.0f} MB attachment limit — "
            "it can't be attached to the reply.",
            [],
        )
    return f"Attached '{rel}' — it will be uploaded with the reply.", [str(path)]


def _backup_metadata(target: Path, project: str) -> None:
    """Snapshot the current metadata.md before it is overwritten, so a careless
    whole-file replace is recoverable. Best-effort — a backup failure never blocks
    the write. History lives under the workspace (writable), one timestamped copy
    per overwrite: ``<workspace>/metadata_history/<project>/<UTC>.md``."""
    try:
        hist_dir = config.WORKSPACE_ROOT / "metadata_history" / project
        hist_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        dest = hist_dir / f"{ts}.md"
        n = 1
        while dest.exists():            # multiple overwrites within the same second
            dest = hist_dir / f"{ts}-{n}.md"
            n += 1
        shutil.copy2(target, dest)
    except Exception:
        log.exception("metadata backup failed (non-fatal) for project %s", project)


def _write_metadata(project: str, content: str) -> str:
    """Overwrite ``<calculations-root>/<project>/metadata.md`` with ``content``.

    This is the agent's *only* write surface. It is deliberately narrow: the
    target is always the literal file ``metadata.md`` directly inside an existing
    top-level project directory under the calculations root — never any other
    file, never a nested path, never a directory we'd have to create. The project
    directory must already exist (so the agent can record metadata for any current
    or future project without this tool ever being able to mint new directories or
    touch calculation data). Everything else under the calculations root stays
    read-only.
    """
    # Reject path tricks early: the project name must be a single component, so a
    # caller can't smuggle in '..', a nested 'a/b', or an absolute path.
    if not project or "/" in project or "\\" in project or project in (".", ".."):
        return f"Error: '{project}' is not a valid project name (use a single project directory name)."

    try:
        target = (config.CALCULATIONS_ROOT / project / "metadata.md").resolve()
        target.relative_to(config.CALCULATIONS_ROOT)  # raises if it escapes the root
    except (ValueError, OSError):
        return f"Error: '{project}' resolves outside the calculations root."

    # Enforce shape: <root>/<project>/metadata.md and nothing deeper.
    if target.name != "metadata.md" or target.parent.parent != config.CALCULATIONS_ROOT:
        return f"Error: refusing to write outside a top-level project's metadata.md (got '{project}')."

    if not target.parent.is_dir():
        return (
            f"Error: project '{project}' does not exist under the calculations root. "
            "metadata.md can only be written inside an existing project directory."
        )

    data = content.encode("utf-8")
    if len(data) > config.MAX_FILE_BYTES:
        return (
            f"Error: content is {len(data)} bytes, over the "
            f"{config.MAX_FILE_BYTES}-byte metadata limit. Keep metadata.md concise."
        )

    existed = target.exists()
    if existed:
        _backup_metadata(target, project)   # snapshot the version we're about to clobber
    try:
        # Atomic replace so an interrupted write can't leave a half-written file.
        tmp = target.with_suffix(".md.tmp")
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(content)
        os.replace(tmp, target)
    except OSError as exc:
        return f"Error: could not write '{project}/metadata.md': {exc}."

    verb = "Updated" if existed else "Created"
    rel = target.relative_to(config.CALCULATIONS_ROOT)
    return f"{verb} {rel} ({len(data)} bytes)."


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
# Tool specs — single source of truth for the agent
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
        "name": "attach_file",
        "description": (
            "Attach a file from the calculations root to your Slack reply so the "
            "user receives it as a downloadable file alongside your text. Use this "
            "when the user asks for a file directly, or when handing over a specific "
            "output/data/structure file is more useful than pasting its contents. "
            "Any file type works. The path is relative to the calculations root "
            "(same as read_file). Call once per file; your text reply is still sent."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Path of the file to attach, relative to the calculations root.",
                }
            },
            "required": ["path"],
        },
        "impl": lambda inp, _ctx: _attach_file(inp["path"]),
    },
    {
        "name": "write_metadata",
        "description": (
            "Create or overwrite the metadata.md file in a project's top-level "
            "directory under the calculations root. This is your ONLY way to write "
            "files — it can touch nothing but each project's metadata.md, and all "
            "other calculation data stays read-only. Use it to record or update a "
            "project's metadata (e.g. notes, status, the list of Python libraries "
            "available for analysis). The write replaces the whole file, so read the "
            "current metadata.md first (read_file) and pass the complete new contents. "
            "The project directory must already exist; this cannot create new projects."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "project": {
                    "type": "string",
                    "description": (
                        "Name of the top-level project directory under the "
                        "calculations root (e.g. 'thermolysin'). A single directory "
                        "name, not a path."
                    ),
                },
                "content": {
                    "type": "string",
                    "description": "Full Markdown contents to write to that project's metadata.md.",
                },
            },
            "required": ["project", "content"],
        },
        "impl": lambda inp, _ctx: (_write_metadata(inp["project"], inp["content"]), []),
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

# name → impl(input, context) -> (text, attachments)
TOOL_FNS = {s["name"]: s["impl"] for s in TOOL_SPECS}


def dispatch(name: str, tool_input: dict, context: dict) -> str:
    """
    Run a tool by name, draining any attachment paths into the per-turn sink
    (``context["attachments"]``), and return just the tool-result text.
    """
    attachments = context.setdefault("attachments", [])
    fn = TOOL_FNS.get(name)
    if fn is None:
        return f"Unknown tool: {name}"
    result_text, atts = fn(tool_input, context)
    attachments.extend(atts)
    return result_text
