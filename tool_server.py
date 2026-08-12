#!/usr/bin/env python3
"""
Aspen FastAPI Tool Server
Handles sandboxed bubblewrap (bwrap) execution of LLM-generated analysis code.
Binds to 127.0.0.1 only — not reachable from outside the node.
"""

import ast
import functools
import hashlib
import json
import logging
import os
import re
import shutil
import sqlite3
import subprocess
import tempfile
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
SQLITE_DB_ROOT        = Path(os.getenv("SQLITE_DB_ROOT", str(WORKSPACE_ROOT / "db"))).resolve()
SQLITE_USE_WAL        = os.getenv("SQLITE_USE_WAL", "false").lower() == "true"

FIGURES_DIR           = WORKSPACE_ROOT / "figures"
FIGURE_ARCHIVE_DIR    = WORKSPACE_ROOT / "figure_archive"
CACHE_DIR             = WORKSPACE_ROOT / "cache"
LOGS_DIR              = WORKSPACE_ROOT / "logs"
GENERATED_CODE_DIR    = WORKSPACE_ROOT / "generated_code"

# --- Per-user calculations roots -------------------------------------------
# PROJECTS_ROOT above is the default; each registered user may have their own
# (`calc_root` in the registry) and there may be shared group roots. The registry
# is read DIRECTLY here rather than importing the aspen package: this process
# deliberately has no Slack/model configuration, and it must resolve the root
# itself so the caller can never pass one over the socket. Only a user_id crosses
# that boundary; the mapping user -> root is ours.
_STATE_DIR            = Path(os.getenv("ASPEN_STATE_DIR", str(Path.home() / ".aspen")))
USERS_FILE            = Path(os.getenv("ASPEN_USERS_FILE", str(_STATE_DIR / "users.json")))
METADATA_ROOT         = Path(os.getenv("ASPEN_METADATA_ROOT", str(_STATE_DIR / "metadata"))).resolve()


def _shared_roots() -> dict:
    out = {}
    for item in os.getenv("ASPEN_SHARED_CALC_ROOTS", "").split(","):
        label, sep, path = item.partition("=")
        if sep and label.strip() and path.strip():
            out[label.strip().lower()] = Path(path.strip()).resolve()
    return out


SHARED_CALC_ROOTS     = _shared_roots()

# DEMO mode (see aspen/demo.py). A demo visitor is not in the registry, so
# without this they would resolve to PROJECTS_ROOT — the real tree — and naming a
# project that exists there would mount real research data into the jail. The
# in-process read tools are fenced by aspen/roots.py, but this process is
# separate and knows nothing about demo sessions, so the bot has to say so and
# this has to honor it. Demo analysis is confined to the demo tree and nothing
# else.
DEMO_ROOT             = Path(os.getenv(
    "ASPEN_DEMO_ROOT", str(Path(__file__).resolve().parent / "examples" / "demo-calculations")
)).resolve()
# The import advisory used when a project states no list of its own. It was once
# the demo tree's special case — the demo carries no notes, and requiring them
# would have made its payoff beat 422 instead of plotting. That reasoning was
# never specific to the demo: no project's notes are required to analyse it (see
# load_metadata), so every project without a list gets this one.
DEFAULT_LIBRARIES     = ["numpy", "pandas", "matplotlib", "scipy"]

MAX_STDOUT_CHARS      = int(os.getenv("MAX_STDOUT_CHARS", "10000"))
MAX_STDERR_CHARS      = int(os.getenv("MAX_STDERR_CHARS", "2000"))
MAX_FIGURE_BYTES      = int(os.getenv("MAX_FIGURE_BYTES", str(5 * 1024 * 1024)))
FIGURE_ARCHIVE_MAX    = int(os.getenv("FIGURE_ARCHIVE_MAX_BYTES", str(2 * 1024 ** 3)))
FIGURE_ARCHIVE_TRIM   = int(os.getenv("FIGURE_ARCHIVE_TRIM_BYTES", str(int(1.5 * 1024 ** 3))))
EXECUTION_TIMEOUT     = int(os.getenv("EXECUTION_TIMEOUT_SECONDS", "120"))

# --- Sandbox runtime: bubblewrap (bwrap) -----------------------------------
# Analysis code runs under bwrap: no network, a minimal read-only filesystem, the
# project mounted read-only, and only the workspace figures/cache writable. We
# resolve the binaries to absolute paths because the sandbox is launched with a
# scrubbed environment — the PATH in that env must not get to decide what runs.
BWRAP_BIN   = shutil.which(os.getenv("BWRAP_BIN", "bwrap")) or "/usr/bin/bwrap"
PRLIMIT_BIN = shutil.which("prlimit") or "/usr/bin/prlimit"
# Python interpreter (a venv) holding the analysis libraries. start.sh bootstraps
# this venv; the default matches its ANALYSIS_VENV path.
ANALYSIS_PYTHON = os.getenv(
    "ANALYSIS_PYTHON", str(WORKSPACE_ROOT / "analysis-venv" / "bin" / "python")
)
# Host paths bind-mounted read-only so the interpreter + its compiled deps resolve
# their shared libraries. Deliberately NOT /home, /etc (beyond the loader files
# bound below), other projects, or anything secret.
ANALYSIS_RO_PATHS = [
    p.strip() for p in
    os.getenv("ANALYSIS_RO_PATHS", "/usr,/lib,/lib64,/bin,/sbin").split(",")
    if p.strip()
]
# Hard per-task resource caps. bwrap has no cgroup/memory limits, and this host is
# cgroups v1 (so rootless Apptainer --memory could not work either), so we enforce
# caps with prlimit. RLIMIT_AS bounds *virtual* address space — set generously,
# since BLAS/numpy reserve large virtual arenas; the wall-clock EXECUTION_TIMEOUT
# is the primary backstop. Any limit set to 0 is disabled.
ANALYSIS_AS_LIMIT_BYTES    = int(os.getenv("ANALYSIS_AS_LIMIT_BYTES", str(2 * 1024 ** 3)))
ANALYSIS_FSIZE_LIMIT_BYTES = int(os.getenv("ANALYSIS_FSIZE_LIMIT_BYTES", str(512 * 1024 ** 2)))
ANALYSIS_CPU_LIMIT_SECONDS = int(os.getenv("ANALYSIS_CPU_LIMIT_SECONDS", str(EXECUTION_TIMEOUT)))

# Scrubbed environment for the sandboxed process (bwrap 0.4.0 has no --clearenv, so
# we hand the subprocess only these vars; bwrap forwards exactly them). No secrets.
SANDBOX_ENV = {
    "PATH": "/usr/bin:/bin",
    "HOME": "/tmp",
    "TMPDIR": "/tmp",
    # matplotlib's font cache must go somewhere the import hook permits writing
    # (figures/ or cache/), not /tmp — and cache/ persists across runs, so the
    # font list is built once instead of every invocation.
    "MPLCONFIGDIR": "/aspen_workspace/cache/mpl",
    "MPLBACKEND": "Agg",          # headless plotting
    "PYTHONDONTWRITEBYTECODE": "1",
    "PYTHONUNBUFFERED": "1",
    "LC_ALL": "C.UTF-8",
    "LANG": "C.UTF-8",
    # Keep BLAS/OpenMP thread pools small so memory/CPU stay within the caps.
    "OMP_NUM_THREADS": "2",
    "OPENBLAS_NUM_THREADS": "2",
    "MKL_NUM_THREADS": "2",
    "NUMEXPR_NUM_THREADS": "2",
}

# --- Analysis jail seccomp filter (defense-in-depth on the kernel surface) -----
# bwrap confines what the analysis code can *reach*; seccomp confines which
# syscalls it can issue to the (old) kernel — blocking the obscure, never-needed
# ones that are the usual road to kernel privilege-escalation. Default-ALLOW with a
# denylist: a strict allowlist is too brittle for arbitrary numeric Python. The
# compiled BPF is built ONCE at startup (needs pyseccomp + libseccomp); if the
# binding is missing the jail still runs WITHOUT the filter (logged loudly) — the
# bind-mount/namespace boundary is unaffected. Toggle with ANALYSIS_SECCOMP.
ANALYSIS_SECCOMP = os.getenv("ANALYSIS_SECCOMP", "true").lower() in ("1", "true", "yes")

# name -> errno on attempt. ENOSYS (not EPERM) for clone3/io_uring so glibc/libs
# fall back cleanly rather than erroring; everything else EPERM. Names unknown on
# this kernel/arch are skipped at build time. Verified transparent to
# numpy/pandas/scipy/matplotlib/py3Dmol on this host.
_SECCOMP_DENY = {
    "clone3": "ENOSYS", "io_uring_setup": "ENOSYS",
    "io_uring_enter": "ENOSYS", "io_uring_register": "ENOSYS",
}
_SECCOMP_DENY.update({s: "EPERM" for s in (
    # new-identity / namespace / mount-escape primitives
    "unshare", "setns", "mount", "umount2", "pivot_root", "chroot",
    "move_mount", "open_tree", "fsopen", "fsconfig", "fsmount", "fspick",
    # kernel keyrings (classic LPE foothold)
    "add_key", "request_key", "keyctl",
    # cross-process / debug
    "ptrace", "process_vm_readv", "process_vm_writev",
    # kernel modules / system control
    "init_module", "finit_module", "delete_module", "kexec_load", "kexec_file_load",
    "bpf", "perf_event_open", "reboot", "swapon", "swapoff",
    "settimeofday", "clock_settime", "clock_adjtime", "adjtimex",
    "sethostname", "setdomainname", "acct", "quotactl", "_sysctl",
    # memory-corruption primitive
    "userfaultfd",
)})


def _build_seccomp_bpf() -> Optional[bytes]:
    """Compile the denylist to a BPF program once at startup. Returns the program
    bytes, or None if seccomp is disabled or the binding is unavailable (the jail
    then runs without the filter; the bind-mount/namespace boundary still holds).
    Note: we only EXPORT the program — never ``load()`` it — so the tool server
    process itself is not filtered."""
    if not ANALYSIS_SECCOMP:
        log.info("Analysis seccomp filter disabled via ANALYSIS_SECCOMP.")
        return None
    try:
        import errno as _errno
        import pyseccomp as _seccomp
    except Exception as exc:
        log.warning("pyseccomp unavailable (%s) — analysis jail runs WITHOUT a "
                    "seccomp filter; bwrap/namespace isolation still applies.", exc)
        return None
    flt = _seccomp.SyscallFilter(defaction=_seccomp.ALLOW)
    applied = 0
    for name, errname in _SECCOMP_DENY.items():
        try:
            flt.add_rule(_seccomp.ERRNO(getattr(_errno, errname)), name)
            applied += 1
        except Exception:
            pass  # syscall unknown on this kernel/arch — skip
    with tempfile.TemporaryFile() as tf:
        flt.export_bpf(tf)
        tf.seek(0)
        blob = tf.read()
    log.info("Analysis seccomp filter compiled: %d rules, %d bytes.", applied, len(blob))
    return blob


_SECCOMP_BPF = _build_seccomp_bpf()

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
@functools.lru_cache(maxsize=8)
def _registry_users(stamp: tuple) -> list:
    """Parsed registry entries, re-read when the file changes (stamp is the key)."""
    try:
        raw = json.loads(USERS_FILE.read_text())
    except (OSError, ValueError):
        return []
    return [u for u in (raw.get("users") or []) if isinstance(u, dict)]


def _users() -> list:
    try:
        st = USERS_FILE.stat()
        stamp = (st.st_mtime_ns, st.st_size)
    except OSError:
        return []
    return _registry_users(stamp)


def resolve_scope(user_id: str, owner: str = "", demo: bool = False) -> tuple[Path, str]:
    """(root, scope-directory-name) for a request, resolved from the registry.

    ``owner`` is a *name* (alias / Slack ID / shared root), never a path — the
    agent cannot hand this process a directory to mount. An unknown or empty
    owner falls back to the caller's own root, and a caller we don't know falls
    back to PROJECTS_ROOT, which is exactly the single-root behavior.
    """
    # A demo session is confined to the demo tree, whatever it names. The flag
    # comes from the bot's own session state, never from the model.
    if demo:
        return DEMO_ROOT, ""

    token = (owner or "").strip().lstrip("@").lower()
    if token and token in SHARED_CALC_ROOTS:
        return SHARED_CALC_ROOTS[token], f"_shared__{token}"

    users = _users()
    wanted = token or (user_id or "").strip()
    for entry in users:
        uid = str(entry.get("slack_user_id", ""))
        alias = str(entry.get("alias", "")).lower()
        if wanted and wanted not in (uid, uid.lower(), alias):
            continue
        declared = str(entry.get("calc_root") or "").strip()
        root = Path(declared).resolve() if declared else PROJECTS_ROOT
        return root, f"{alias}__{uid}"
    return PROJECTS_ROOT, ""


def _safe_project_path(project_name: str, root: Optional[Path] = None) -> Path:
    """
    Resolve project_name against ``root`` (the caller's calculations root) and
    confirm it doesn't escape via '..' or symlinks. Raises HTTPException(400).
    """
    base = (root or PROJECTS_ROOT).resolve()
    try:
        resolved = (base / project_name).resolve()
        resolved.relative_to(base)  # raises ValueError if outside
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
def _parse_markdown_metadata(text: str, project_name: str) -> dict:
    """
    Best-effort parse of metadata.md. The document as a whole is the agent-facing
    project description; the only field the server needs is the advisory list of
    Python libraries, taken from the markdown list under a heading mentioning
    "librar" (e.g. "## Python libraries available for analysis").
    """
    libs: list[str] = []
    in_libs = False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            in_libs = bool(re.search(r"librar", stripped, re.IGNORECASE))
            continue
        if in_libs:
            m = re.match(r"[-*]\s+(.+)", stripped)
            if m:
                item = m.group(1).strip().strip("`").strip()
                # Skip placeholder/prompt bullets like _(add ...)_ or <fill in>.
                if item and item[0] not in "_(<" and "fill in" not in item.lower():
                    libs.append(item)
    if not libs:
        libs = list(DEFAULT_LIBRARIES)
    return {"name": project_name, "allowed_libraries": libs, "description": text}


def _sidecar_metadata(scope_dir: str, project_name: str) -> Optional[Path]:
    """Aspen's own metadata for a project, kept outside the calculations tree.

    This is where metadata lives now (see aspen/metadata.py); the in-tree files
    below are the pre-migration fallback, still readable so an existing project
    keeps working until its notes are moved.
    """
    if not scope_dir:
        return None
    try:
        candidate = (METADATA_ROOT / scope_dir / project_name / "metadata.md").resolve()
        candidate.relative_to(METADATA_ROOT)
    except (ValueError, OSError):
        return None
    return candidate if candidate.is_file() else None


# A project's own description, if its owner wrote one. README is the name people
# already use for "what this directory is", and it is the one Aspen asks for now
# (aspen/metadata.py) — the bot cannot write into a calculations root, so the
# in-tree file has to be one the scientist would plausibly write by hand.
_README_NAMES = ("README.md", "README.txt", "README", "readme.md", "readme.txt", "readme")


def _in_tree_notes(project_path: Path) -> Optional[Path]:
    """The first in-tree file describing this project, if any."""
    for name in ("metadata.md", *_README_NAMES):
        candidate = project_path / name
        if candidate.is_file():
            return candidate
    return None


def load_metadata(project_path: Path, scope_dir: str = "", demo: bool = False) -> dict:
    """
    Load project metadata. Prefers Aspen's sidecar, then an in-tree metadata.md or
    README (natural-language markdown), then metadata.toml / metadata.yaml.

    **Notes are never required.** A project with none analyses fine, on the
    DEFAULT_LIBRARIES advisory. This used to raise 422 with a template telling the
    user to "create metadata.md in the project root" — an instruction that stopped
    being possible once metadata moved out of the tree (the agent writes nothing
    inside any calculations root), and which blocked analysis of every project
    nobody had documented yet. Encouraging a description is now a *nudge* on a
    directory listing, not a precondition for running anything: see
    aspen/metadata.py:describes and aspen/tools.py:_list_directory.

    A file that exists but is malformed still raises — that is a broken file the
    owner should hear about, which is a different thing from no file at all.
    """
    if demo:
        return {"name": project_path.name, "allowed_libraries": list(DEFAULT_LIBRARIES),
                "description": "Synthetic demo project."}

    sidecar = _sidecar_metadata(scope_dir, project_path.name)
    if sidecar is not None:
        return _parse_markdown_metadata(sidecar.read_text(), project_path.name)

    in_tree = _in_tree_notes(project_path)
    if in_tree is not None:
        return _parse_markdown_metadata(
            in_tree.read_text(errors="replace"), project_path.name
        )

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

    return {"name": project_path.name, "allowed_libraries": list(DEFAULT_LIBRARIES),
            "description": ""}


def _validate_metadata(metadata: dict) -> list[str]:
    """Return the import advisory, falling back to the default list.

    Only a structured metadata.toml / .yaml can reach here without the field —
    every other path fills it in. An old file that predates the field is not a
    reason to refuse to analyse the project, so it gets the same default as a
    project with no notes at all.
    """
    libs = metadata.get("allowed_libraries")
    if not libs or not isinstance(libs, list):
        return list(DEFAULT_LIBRARIES)
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
# ---- Aspen security hook (defense-in-depth; the bwrap jail is the real boundary) ----
# Network isolation is enforced by bwrap's --unshare-all (no network namespace).
# Project data is bind-mounted read-only and the rest of the filesystem is read-only
# too; only the workspace figures/cache are writable. This hook is a redundant
# write-path guard so generated code cannot write outside the designated outputs.
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
# Sandboxed (bwrap) execution
# ---------------------------------------------------------------------------
@functools.lru_cache(maxsize=1)
def _python_bind_paths() -> tuple[str, ...]:
    """Read-only host paths the analysis interpreter needs: its venv prefix and the
    base CPython prefix(es) it was created from. We bind all four of sys.{prefix,
    exec_prefix,base_prefix,base_exec_prefix} because a uv-managed CPython (as here)
    splits them — the actual python3.x binary lives under base_exec_prefix while the
    venv symlink/stdlib reference base_prefix, and missing either breaks exec. Also
    resolves the interpreter's real path (the symlink target) for good measure.
    Discovered by asking the interpreter, so it works wherever they live (not /usr).
    Cached; falls back to the interpreter's parent prefix if introspection fails."""
    try:
        out = subprocess.run(
            [ANALYSIS_PYTHON, "-c",
             "import sys,os;"
             "print(sys.prefix,sys.exec_prefix,sys.base_prefix,sys.base_exec_prefix,"
             "os.path.realpath(sys.executable),sep='\\n')"],
            capture_output=True, text=True, timeout=10, check=True,
        ).stdout.split("\n")
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("Could not introspect ANALYSIS_PYTHON (%s); binding its parent prefix", exc)
        out = [str(Path(ANALYSIS_PYTHON).resolve().parent.parent)]
    # Bind the LITERAL prefix paths (do NOT resolve them): the venv's python symlink
    # points at the unresolved base path, so that exact path must exist in the jail.
    # The final entry is the realpath of the executable — resolved, so we also cover
    # wherever the symlink chain actually lands. A file entry binds its parent dir.
    paths: list[str] = []
    for p in out:
        p = p.strip()
        if not p:
            continue
        if os.path.isfile(p):
            p = str(Path(p).parent)
        if p and p != "/" and p not in paths:
            paths.append(p)
    return tuple(paths)


def build_sandbox_cmd(script_path: Path, project_name: str, project_path: Path,
                      seccomp_fd: Optional[int] = None) -> list[str]:
    """Assemble the ``prlimit`` + ``bwrap`` argv that runs the analysis script in a
    no-network, read-only-filesystem jail. Pure (no side effects) so it is unit-testable.

    bwrap gives us: all namespaces unshared (``--unshare-all`` — includes the network
    namespace, so no network), the project mounted read-only at /projects/<name>, only
    the workspace figures/cache writable, a tmpfs /tmp, and nothing else from the host
    except the read-only system + interpreter paths needed to run Python. prlimit adds
    the per-task resource caps bwrap cannot (this host is cgroups v1)."""
    bwrap = [
        BWRAP_BIN,
        "--unshare-all",        # all namespaces incl. network -> no network access
        "--die-with-parent",    # jail is torn down if the tool server dies
        "--new-session",        # own session: blocks TIOCSTI terminal-injection tricks
        "--proc", "/proc",
        "--dev", "/dev",
        "--tmpfs", "/tmp",
    ]
    # Seccomp syscall filter (the kernel-surface guard). Applied to the analysis
    # process after bwrap's own setup, so blocking unshare/mount/clone3 here doesn't
    # hinder the jail itself. Omitted when no filter is available.
    if seccomp_fd is not None:
        bwrap += ["--seccomp", str(seccomp_fd)]
    # System libraries / interpreters, read-only. -try so a missing path is skipped
    # rather than fatal. No /home, no /etc secrets, no other projects.
    for p in ANALYSIS_RO_PATHS:
        bwrap += ["--ro-bind-try", p, p]
    # Dynamic-loader config so compiled deps (libgfortran, libgomp, ...) resolve,
    # plus fontconfig config so matplotlib finds system fonts (and stops warning
    # "Cannot load default config file"); the fonts themselves live under /usr.
    for p in ("/etc/ld.so.cache", "/etc/ld.so.conf", "/etc/ld.so.conf.d", "/etc/fonts"):
        bwrap += ["--ro-bind-try", p, p]
    # The analysis interpreter's venv + base CPython prefixes.
    for p in _python_bind_paths():
        bwrap += ["--ro-bind", p, p]
    bwrap += [
        "--ro-bind", str(project_path), f"/projects/{project_name}",   # project: read-only
        "--bind", str(FIGURES_DIR), "/aspen_workspace/figures",        # outputs: read-write
        "--bind", str(CACHE_DIR), "/aspen_workspace/cache",
        "--ro-bind", str(script_path), "/aspen_script.py",
        "--chdir", "/tmp",
        ANALYSIS_PYTHON, "/aspen_script.py",
    ]
    # prlimit resource caps (bwrap has no cgroup limits; this host is cgroups v1).
    limits = []
    if ANALYSIS_AS_LIMIT_BYTES:
        limits.append(f"--as={ANALYSIS_AS_LIMIT_BYTES}")
    if ANALYSIS_CPU_LIMIT_SECONDS:
        limits.append(f"--cpu={ANALYSIS_CPU_LIMIT_SECONDS}")
    if ANALYSIS_FSIZE_LIMIT_BYTES:
        limits.append(f"--fsize={ANALYSIS_FSIZE_LIMIT_BYTES}")
    return [PRLIMIT_BIN, *limits, "--", *bwrap] if limits else bwrap


def run_in_bwrap(
    script_path: Path,
    project_name: str,
    project_path: Path,
) -> dict:
    """
    Run script_path under a bwrap sandbox with:
    - no network access (--unshare-all unshares the network namespace)
    - scrubbed environment (SANDBOX_ENV only — no secrets)
    - project directory mounted read-only; rest of the filesystem read-only
    - figures and cache directories mounted read-write (the only writable host paths)
    - per-task resource caps via prlimit (RLIMIT_AS / CPU / FSIZE)
    - EXECUTION_TIMEOUT second hard kill

    Returns dict with stdout, stderr, figures, status, duration_seconds.
    """
    figures_before = set(FIGURES_DIR.glob("*.png"))

    # Materialize the compiled seccomp BPF into an inheritable fd for bwrap's
    # --seccomp. A per-call file (not a shared one) so concurrent runs don't race
    # on a single fd's read offset.
    seccomp_file = None
    seccomp_fd = None
    if _SECCOMP_BPF is not None:
        seccomp_file = tempfile.TemporaryFile()
        seccomp_file.write(_SECCOMP_BPF)
        seccomp_file.flush()
        seccomp_file.seek(0)
        os.set_inheritable(seccomp_file.fileno(), True)
        seccomp_fd = seccomp_file.fileno()

    cmd = build_sandbox_cmd(script_path, project_name, project_path, seccomp_fd=seccomp_fd)
    pass_fds = (seccomp_fd,) if seccomp_fd is not None else ()

    log.info("Launching bwrap sandbox for project=%s script=%s", project_name, script_path.name)
    t0 = time.monotonic()

    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=EXECUTION_TIMEOUT,
            env=SANDBOX_ENV,           # scrubbed env — bwrap forwards exactly these vars
            pass_fds=pass_fds,         # hand bwrap the compiled seccomp BPF (if any)
        )
        duration = time.monotonic() - t0
        stdout = filter_secrets(proc.stdout)
        stderr = filter_secrets(proc.stderr)
        status = "success" if proc.returncode == 0 else "error"
        # SIGKILL/SIGXCPU (-9 / -24 / 137 / 152) almost always means a prlimit cap was
        # hit; surface a hint since that leaves little stderr. (An RLIMIT_AS overrun
        # usually instead shows up as a Python MemoryError in stderr.)
        if proc.returncode in (-9, -24, 137, 152):
            note = (
                "Process killed — likely hit a resource cap "
                f"(memory/AS={ANALYSIS_AS_LIMIT_BYTES} B, CPU={ANALYSIS_CPU_LIMIT_SECONDS}s)."
            )
            stderr = f"{stderr}\n{note}".strip() if stderr else note

    except subprocess.TimeoutExpired as exc:
        duration = time.monotonic() - t0
        stdout = filter_secrets(exc.stdout or "")
        stderr = filter_secrets(exc.stderr or "")
        status = "timeout"
        log.warning("bwrap sandbox timed out after %ds for project=%s", EXECUTION_TIMEOUT, project_name)
    finally:
        if seccomp_file is not None:
            seccomp_file.close()

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
    # Whose calculations to read: an alias / Slack ID / shared-root NAME, never a
    # path. resolve_scope turns it into a directory; an unknown name simply falls
    # back to the caller's own root.
    owner: str = ""
    # Set by the BOT from its own session state (never by the model, and never
    # from a tool argument): confine this run to the demo tree.
    demo: bool = False


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

    # Whose calculations — resolved HERE from the registry, never taken as a path.
    root, scope_dir = resolve_scope(req.user_id, req.owner, req.demo)
    project_path = _safe_project_path(project_name, root)

    # Load and validate metadata (Aspen's sidecar first, then in-tree legacy)
    metadata = load_metadata(project_path, scope_dir, req.demo)
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
        result = run_in_bwrap(script_path, project_name, project_path)
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
    # Listen on a Unix-domain socket (a file), not a TCP port: on a shared node a
    # 127.0.0.1 port is reachable by every local user, whereas a socket in a 0700
    # dir is reachable only by this user. The shared-secret header stays as a
    # second layer (see run_python_analysis).
    sock_path = Path(
        os.getenv("ASPEN_TOOL_SERVER_SOCKET", str(WORKSPACE_ROOT / "run" / "tool.sock"))
    )
    sock_path.parent.mkdir(parents=True, exist_ok=True)
    # The 0700 directory is the real access control. uvicorn always chmods the
    # socket itself to 0666 (after bind, with no hook to override), but connecting
    # to a unix socket also requires search permission on its parent dir — and no
    # other user can enter a 0700 dir they don't own, so the socket is unreachable
    # to them regardless of its own mode.
    os.chmod(sock_path.parent, 0o700)
    try:
        sock_path.unlink()                   # remove a stale socket from a prior run
    except FileNotFoundError:
        pass
    log.info("Tool server listening on unix socket %s", sock_path)
    uvicorn.run(app, uds=str(sock_path), workers=1)
