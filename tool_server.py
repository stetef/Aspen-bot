#!/usr/bin/env python3
"""
Aspen FastAPI Tool Server
Handles sandboxed Apptainer execution of LLM-generated analysis code.
Binds to 127.0.0.1 only — not reachable from outside the node.
"""

import ast
import hashlib
import json
import logging
import os
import re
import shutil
import sqlite3
import subprocess
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import tomllib
import yaml
import uvicorn
from dotenv import load_dotenv
from fastapi import FastAPI, Header, HTTPException, Request
from pydantic import BaseModel

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("aspen.tool_server")

# ---------------------------------------------------------------------------
# Configuration — all from .env
# ---------------------------------------------------------------------------
AGENT_INTERNAL_SECRET = os.environ["AGENT_INTERNAL_SECRET"]
PROJECTS_ROOT         = Path(os.environ["PROJECTS_ROOT"]).resolve()
WORKSPACE_ROOT        = Path(os.environ["WORKSPACE_ROOT"]).resolve()
APPTAINER_IMAGE       = os.environ["APPTAINER_IMAGE"]
SQLITE_DB_ROOT        = Path(os.getenv("SQLITE_DB_ROOT", str(WORKSPACE_ROOT / "db"))).resolve()
SQLITE_USE_WAL        = os.getenv("SQLITE_USE_WAL", "false").lower() == "true"

FIGURES_DIR           = WORKSPACE_ROOT / "figures"
FIGURE_ARCHIVE_DIR    = WORKSPACE_ROOT / "figure_archive"
CACHE_DIR             = WORKSPACE_ROOT / "cache"
LOGS_DIR              = WORKSPACE_ROOT / "logs"
GENERATED_CODE_DIR    = WORKSPACE_ROOT / "generated_code"

MAX_STDOUT_CHARS      = int(os.getenv("MAX_STDOUT_CHARS", "10000"))
MAX_STDERR_CHARS      = int(os.getenv("MAX_STDERR_CHARS", "2000"))
MAX_FIGURE_BYTES      = int(os.getenv("MAX_FIGURE_BYTES", str(5 * 1024 * 1024)))
FIGURE_ARCHIVE_MAX    = int(os.getenv("FIGURE_ARCHIVE_MAX_BYTES", str(2 * 1024 ** 3)))
FIGURE_ARCHIVE_TRIM   = int(os.getenv("FIGURE_ARCHIVE_TRIM_BYTES", str(int(1.5 * 1024 ** 3))))
EXECUTION_TIMEOUT     = int(os.getenv("EXECUTION_TIMEOUT_SECONDS", "120"))
# Hard per-task memory cap for the analysis sandbox. Enforced via Apptainer's
# --memory/--memory-swap (needs cgroups v2 delegation, rootless, or root). Set to
# an empty string to disable the cap.
APPTAINER_MEMORY_LIMIT = os.getenv("APPTAINER_MEMORY_LIMIT", "1G")

for _d in [FIGURES_DIR, FIGURE_ARCHIVE_DIR, CACHE_DIR, LOGS_DIR,
           GENERATED_CODE_DIR, SQLITE_DB_ROOT]:
    _d.mkdir(parents=True, exist_ok=True)

app = FastAPI(title="Aspen Tool Server", docs_url=None, redoc_url=None)

# ---------------------------------------------------------------------------
# Secret patterns to redact from stdout/stderr
# ---------------------------------------------------------------------------
_SECRET_RE = re.compile(
    r".*(SLACK_BOT_TOKEN|SLACK_APP_TOKEN|ANTHROPIC_API_KEY|AGENT_INTERNAL_SECRET"
    r"|xoxb-|xapp-|sk-ant-).*",
    re.IGNORECASE,
)


def filter_secrets(text: str) -> str:
    """Redact lines containing known secret patterns."""
    return "\n".join(
        "[REDACTED BY ASPEN LOG FILTER]" if _SECRET_RE.match(line) else line
        for line in text.split("\n")
    )


# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------
def _require_secret(x_agent_secret: str = Header(..., alias="x-agent-secret")) -> None:
    if x_agent_secret != AGENT_INTERNAL_SECRET:
        raise HTTPException(status_code=403, detail="Forbidden")


# ---------------------------------------------------------------------------
# Path validation
# ---------------------------------------------------------------------------
def _safe_project_path(project_name: str) -> Path:
    """
    Resolve project_name against PROJECTS_ROOT and confirm it doesn't escape
    via '..' or symlinks. Raises HTTPException(400) if invalid.
    """
    try:
        resolved = (PROJECTS_ROOT / project_name).resolve()
        resolved.relative_to(PROJECTS_ROOT)  # raises ValueError if outside
    except (ValueError, OSError):
        raise HTTPException(status_code=400, detail=f"Invalid project name: {project_name!r}")
    if not resolved.is_dir():
        raise HTTPException(status_code=400, detail=f"Project directory not found: {project_name!r}")
    return resolved


def _safe_run_path(project_path: Path, run_name: str) -> Path:
    """Resolve run_name within project_path. Raises HTTPException(400) if invalid."""
    try:
        resolved = (project_path / run_name).resolve()
        resolved.relative_to(project_path)
    except (ValueError, OSError):
        raise HTTPException(status_code=400, detail=f"Invalid run directory: {run_name!r}")
    return resolved


# ---------------------------------------------------------------------------
# Metadata loading
# ---------------------------------------------------------------------------
_METADATA_TEMPLATE = """\
```toml
name = "your_project_name"
allowed_libraries = ["numpy", "pandas", "matplotlib"]

[parsers]
energy = "energy.csv"
output_files = ["energy.csv", "log.txt"]

[datasets]
run_group_1 = ["run_001", "run_002"]
```"""


def load_metadata(project_path: Path) -> dict:
    """
    Load metadata.toml or metadata.yaml from the project root.
    Returns the parsed dict, or raises HTTPException(422) with a helpful message.
    """
    toml_path = project_path / "metadata.toml"
    yaml_path = project_path / "metadata.yaml"

    if toml_path.exists():
        try:
            return tomllib.loads(toml_path.read_text())
        except Exception as e:
            raise HTTPException(
                status_code=422,
                detail=f"Malformed metadata.toml in {project_path.name}: {e}",
            )
    if yaml_path.exists():
        try:
            data = yaml.safe_load(yaml_path.read_text())
            if not isinstance(data, dict):
                raise ValueError("Expected a YAML mapping at the top level")
            return data
        except Exception as e:
            raise HTTPException(
                status_code=422,
                detail=f"Malformed metadata.yaml in {project_path.name}: {e}",
            )

    raise HTTPException(
        status_code=422,
        detail=(
            f"No metadata.toml or metadata.yaml found in {project_path.name}/. "
            f"Please create one. Template:\n{_METADATA_TEMPLATE}"
        ),
    )


def _validate_metadata(metadata: dict) -> list[str]:
    """Return allowed_libraries from metadata, raising HTTPException if missing."""
    libs = metadata.get("allowed_libraries")
    if not libs or not isinstance(libs, list):
        raise HTTPException(
            status_code=422,
            detail=(
                "metadata file is missing required field 'allowed_libraries' "
                "(must be a list of strings)."
            ),
        )
    return [str(lib) for lib in libs]


# ---------------------------------------------------------------------------
# Static code safety check
# ---------------------------------------------------------------------------
def check_code_safety(code: str) -> Optional[str]:
    """
    Returns an error message if code contains exec() or eval() calls, else None.
    Uses AST analysis — does not execute code.
    """
    if not code or not code.strip():
        return "Code is empty."
    try:
        tree = ast.parse(code)
    except SyntaxError as e:
        return f"Syntax error in generated code: {e}"
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            if node.func.id in ("exec", "eval"):
                return f"Code contains '{node.func.id}()' which is not permitted."
    return None


# ---------------------------------------------------------------------------
# Import hook injection
# ---------------------------------------------------------------------------
_HOOK_TEMPLATE = """\
# ---- Aspen security hook (defense-in-depth; container is the real boundary) ----
# Network isolation is enforced by --net --network none at the container level.
# Project data is bind-mounted read-only. This hook only enforces the write-path
# restriction within the writable workspace so generated code cannot overwrite
# files outside the designated output directories.
import builtins as _builtins
import os.path as _ospath

_WRITABLE = ('/aspen_workspace/figures/', '/aspen_workspace/cache/')
_real_open = _builtins.open
def _aspen_open(file, mode='r', *args, **kwargs):
    if any(c in str(mode) for c in 'wax'):
        resolved = _ospath.realpath(str(file))
        if not any(resolved.startswith(p) for p in _WRITABLE):
            raise PermissionError(
                f"Write access denied: {file}. "
                "Only /aspen_workspace/figures/ and /aspen_workspace/cache/ are writable."
            )
    return _real_open(file, mode, *args, **kwargs)
_builtins.open = _aspen_open
# ---- end Aspen security hook ----

"""


def inject_import_hook(code: str, allowed_libs: list[str]) -> str:
    """Prepend the write-path restriction hook to the generated code."""
    return _HOOK_TEMPLATE + code


# ---------------------------------------------------------------------------
# SQLite connection
# ---------------------------------------------------------------------------
def get_db(project_name: str) -> sqlite3.Connection:
    db_path = SQLITE_DB_ROOT / f"{project_name}.sqlite"
    conn = sqlite3.connect(str(db_path), check_same_thread=False)
    conn.execute("PRAGMA busy_timeout = 5000")
    if SQLITE_USE_WAL:
        conn.execute("PRAGMA journal_mode = WAL")
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS runs (
            run_id      INTEGER PRIMARY KEY AUTOINCREMENT,
            path        TEXT UNIQUE NOT NULL,
            status      TEXT,
            tags        TEXT,
            energy      REAL,
            structure   TEXT,
            last_update TEXT
        );
        CREATE TABLE IF NOT EXISTS datasets (
            dataset_name TEXT PRIMARY KEY,
            run_ids      TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_runs_status ON runs(status);
        CREATE INDEX IF NOT EXISTS idx_runs_tags   ON runs(tags);
    """)
    conn.commit()
    return conn


# ---------------------------------------------------------------------------
# Figure archive cleanup
# ---------------------------------------------------------------------------
def trim_figure_archive() -> None:
    """Delete oldest files in figure_archive if total size exceeds threshold."""
    files = sorted(FIGURE_ARCHIVE_DIR.glob("*.png"), key=lambda p: p.stat().st_mtime)
    total = sum(p.stat().st_size for p in files)
    if total <= FIGURE_ARCHIVE_MAX:
        return
    log.info("Figure archive %.1f GB exceeds limit; trimming.", total / 1024 ** 3)
    for p in files:
        if total <= FIGURE_ARCHIVE_TRIM:
            break
        size = p.stat().st_size
        p.unlink()
        total -= size
        log.info("Deleted archived figure: %s", p.name)


# ---------------------------------------------------------------------------
# Caching
# ---------------------------------------------------------------------------
def _cache_key(question: str, dataset: list[str], project_path: Path) -> str:
    max_mtime = 0.0
    for run in sorted(dataset):
        run_path = project_path / run
        if run_path.exists():
            for f in run_path.rglob("*"):
                if f.is_file():
                    max_mtime = max(max_mtime, f.stat().st_mtime)
    payload = json.dumps(
        {"question": question, "dataset": sorted(dataset), "max_mtime": max_mtime},
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode()).hexdigest()


def cache_read(project_name: str, key: str) -> Optional[dict]:
    cache_file = CACHE_DIR / project_name / f"{key}.json"
    if not cache_file.exists():
        return None
    try:
        entry = json.loads(cache_file.read_text())
        # Verify all referenced figures still exist
        for fig in entry.get("figures", []):
            if not Path(fig).exists():
                log.info("Cache miss — archived figure missing: %s", fig)
                cache_file.unlink(missing_ok=True)
                return None
        return entry
    except Exception:
        return None


def cache_write(project_name: str, key: str, result: dict) -> None:
    cache_dir = CACHE_DIR / project_name
    cache_dir.mkdir(parents=True, exist_ok=True)
    (cache_dir / f"{key}.json").write_text(json.dumps(result))


# ---------------------------------------------------------------------------
# Structured logging
# ---------------------------------------------------------------------------
def write_audit_log(project_name: str, entry: dict) -> None:
    log_dir = LOGS_DIR / project_name
    log_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%d")
    log_file = log_dir / f"{ts}.jsonl"
    with open(log_file, "a") as fh:
        fh.write(json.dumps(entry) + "\n")


# ---------------------------------------------------------------------------
# Apptainer execution
# ---------------------------------------------------------------------------
def run_in_apptainer(
    script_path: Path,
    project_name: str,
    project_path: Path,
) -> dict:
    """
    Run script_path in an Apptainer container with:
    - no network access
    - clean environment (no secrets)
    - project directory mounted read-only
    - figures and cache directories mounted read-write
    - hard memory limit per task (APPTAINER_MEMORY_LIMIT, default 1 GB;
      requires cgroups v2 delegation / rootless or root to be enforced)
    - EXECUTION_TIMEOUT second hard kill

    Returns dict with stdout, stderr, figures, status, duration_seconds.
    """
    figures_before = set(FIGURES_DIR.glob("*.png"))

    cmd = [
        "apptainer", "exec",
        "--cleanenv",
        "--net", "--network", "none",
    ]
    # Hard memory cap (cgroups). --memory-swap == --memory disables swap, so the
    # cap can't be circumvented by swapping.
    if APPTAINER_MEMORY_LIMIT:
        cmd += ["--memory", APPTAINER_MEMORY_LIMIT, "--memory-swap", APPTAINER_MEMORY_LIMIT]
    cmd += [
        # Project data: read-only
        "--bind", f"{project_path}:/projects/{project_name}:ro",
        # Workspace outputs: read-write
        "--bind", f"{FIGURES_DIR}:/aspen_workspace/figures:rw",
        "--bind", f"{CACHE_DIR}:/aspen_workspace/cache:rw",
        # Script: read-only bind of the single file
        "--bind", f"{script_path}:/aspen_script.py:ro",
        APPTAINER_IMAGE,
        "python", "/aspen_script.py",
    ]

    log.info("Launching Apptainer for project=%s script=%s", project_name, script_path.name)
    t0 = time.monotonic()

    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=EXECUTION_TIMEOUT,
        )
        duration = time.monotonic() - t0
        stdout = filter_secrets(proc.stdout)
        stderr = filter_secrets(proc.stderr)
        status = "success" if proc.returncode == 0 else "error"
        # A SIGKILL (-9 / 137) with a memory cap set is almost always an OOM kill;
        # surface a clear hint since cgroup OOM leaves little/no stderr.
        if APPTAINER_MEMORY_LIMIT and proc.returncode in (-9, 137):
            note = f"Process killed — likely exceeded the {APPTAINER_MEMORY_LIMIT} memory limit."
            stderr = f"{stderr}\n{note}".strip() if stderr else note

    except subprocess.TimeoutExpired as exc:
        duration = time.monotonic() - t0
        stdout = filter_secrets(exc.stdout or "")
        stderr = filter_secrets(exc.stderr or "")
        status = "timeout"
        log.warning("Apptainer timed out after %ds for project=%s", EXECUTION_TIMEOUT, project_name)

    # Collect any new figures produced
    figures_after = set(FIGURES_DIR.glob("*.png"))
    new_figures = sorted(str(p) for p in figures_after - figures_before)

    # Check figure sizes — flag oversized ones, don't remove them yet
    oversized = [f for f in new_figures if Path(f).stat().st_size > MAX_FIGURE_BYTES]
    acceptable = [f for f in new_figures if f not in oversized]

    truncated = len(stdout) > MAX_STDOUT_CHARS or len(stderr) > MAX_STDERR_CHARS

    return {
        "status": status,
        "stdout": stdout[:MAX_STDOUT_CHARS],
        "stderr": stderr[:MAX_STDERR_CHARS],
        "figures": acceptable,
        "oversized_figures": oversized,
        "truncated": truncated,
        "duration_seconds": round(duration, 2),
    }


# ---------------------------------------------------------------------------
# Request / Response models
# ---------------------------------------------------------------------------
class AnalysisRequest(BaseModel):
    code: str
    dataset: list[str]
    question: str
    user_id: str = ""
    username: str = ""
    thread_ts: str = ""


# ---------------------------------------------------------------------------
# Main endpoint
# ---------------------------------------------------------------------------
@app.post("/run_python_analysis/{project_name}")
def run_python_analysis(
    project_name: str,
    req: AnalysisRequest,
    x_agent_secret: str = Header(..., alias="x-agent-secret"),
) -> dict:
    # Auth
    if x_agent_secret != AGENT_INTERNAL_SECRET:
        raise HTTPException(status_code=403, detail="Forbidden")

    # Run cleanup before processing
    try:
        trim_figure_archive()
    except Exception:
        log.exception("Figure archive cleanup failed (non-fatal)")

    # Validate project path
    project_path = _safe_project_path(project_name)

    # Load and validate metadata
    metadata = load_metadata(project_path)
    allowed_libs = _validate_metadata(metadata)

    # Validate inputs
    if not req.code or not req.code.strip():
        raise HTTPException(status_code=400, detail="code field is empty")
    if not req.dataset:
        raise HTTPException(status_code=400, detail="dataset list is empty")

    # Validate each run directory exists
    for run in req.dataset:
        run_path = _safe_run_path(project_path, run)
        if not run_path.exists():
            raise HTTPException(
                status_code=400,
                detail=f"Run directory not found: {run!r} in project {project_name!r}",
            )

    # Static safety check for exec/eval
    safety_error = check_code_safety(req.code)
    if safety_error:
        raise HTTPException(status_code=400, detail=safety_error)

    # Cache lookup
    key = _cache_key(req.question, req.dataset, project_path)
    cached = cache_read(project_name, key)
    if cached:
        log.info("Cache hit for project=%s key=%s", project_name, key[:12])
        cached["cache_hit"] = True
        return cached

    # Inject import hook and write script to UUID-named file
    code_with_hook = inject_import_hook(req.code, allowed_libs)
    script_path = GENERATED_CODE_DIR / f"{uuid.uuid4()}.py"
    script_path.write_text(code_with_hook)
    result = {}

    try:
        result = run_in_apptainer(script_path, project_name, project_path)
    finally:
        # Always delete the generated script
        try:
            script_path.unlink()
        except Exception:
            log.exception("Failed to delete generated script %s", script_path)

    result["cache_hit"] = False

    # Save to cache (only on success)
    if result["status"] == "success":
        cache_write(project_name, key, result)

    # Audit log
    log_entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "user_id": req.user_id,
        "username": req.username,
        "thread_ts": req.thread_ts,
        "project": project_name,
        "question": req.question,
        "dataset": req.dataset,
        "generated_code": req.code,         # original, not hook-injected
        "figures": result.get("figures", []),
        "stdout_truncated": result.get("truncated", False),
        "status": result.get("status"),
        "errors": result.get("stderr", ""),
        "duration_seconds": result.get("duration_seconds"),
        "cache_hit": False,
    }
    try:
        write_audit_log(project_name, log_entry)
    except Exception:
        log.exception("Failed to write audit log (non-fatal)")

    return result


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    log.info(
        "Starting Aspen tool server  projects=%s  workspace=%s",
        PROJECTS_ROOT, WORKSPACE_ROOT,
    )
    from urllib.parse import urlparse
    _url = urlparse(os.getenv("TOOL_SERVER_URL", "http://127.0.0.1:27195"))
    uvicorn.run(app, host=_url.hostname or "127.0.0.1", port=_url.port or 27195, workers=1)
