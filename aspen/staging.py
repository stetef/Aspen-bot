"""
Staging structures for a submitted batch — copy, record provenance, never author.

The rule this module exists to enforce is spec §19.3: **the model never authors a
job body.** A submitted job runs unjailed on a compute node as the bot's Unix user,
so *what* runs there is the entire security question, and the answer has to be
structural rather than a matter of validation.

The original design (spec §18.2) said a script path must resolve inside a staging
directory. That is necessary but not sufficient — a tool that accepts a path the
model chose is one writable staging directory away from "the model wrote the job
body". So ``submit_orca_batch`` accepts no script path and no script content at
all. It gets a ``template_mode`` from the enum below and a structure path resolved
through ``roots.resolve`` like every other path-taking tool. The pipeline renders
its own job scripts from its own packaged templates; what reaches a compute node is
pipeline code plus the user's ``.xyz`` data.

Inputs are **copied**, never symlinked. A symlink in staging would let anything
that can write there reach back into the source tree, which would break the
absolute invariant that Aspen writes nothing inside any calculations root.
"""

import hashlib
import json
import logging
import re
import shutil
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from . import config, jobs, roots

log = logging.getLogger("aspen")

# The pipeline's ORCA template modes.
#
# A closed map, because the *only* thing the model contributes to the argv is a key
# of it — an unknown key is refused rather than forwarded, so no wording in a
# conversation can reach the command line.
#
# But it must not be a FROZEN COPY of the pipeline's flags, which is what it was
# first written as. The pipeline gained `--interp` and Aspen went on refusing it,
# reporting a mode the user had just added as one that did not exist. That is the
# two-sources-of-truth desync the design avoids everywhere else (spec §7 rejects an
# index file for exactly this reason), and it fails in the worst direction: silently,
# and against the user who is right.
#
# So this table now supplies only the *names* — which are friendlier than the raw
# flags (`h-only` for `--H`) and are what the tool schema advertises — while the set
# of modes that actually exist is read from the pipeline itself. A flag the pipeline
# stops advertising drops out; one it adds appears under a name derived from the flag.
_CURATED_NAMES = {
    "":                 "ca-fixed",       # the pipeline's default: no flag at all
    "--H":              "h-only",
    "--single":         "single-point",
    "--free":           "free",
    "--backbone":       "backbone",
    "--xtb-free":       "xtb-free",
    "--xtb-constrained": "xtb-constrained",
    "--quick":          "quick",
    "--quick-ca-fixed": "quick-ca-fixed",
    "--interp":         "interp",
}

# Every mode flag in the pipeline's help is described as "…ORCA template…", which is
# how they are told apart from --scheduler, --out-dir, --skip-* and the rest.
_MODE_HELP_MARKER = "orca template"
_MODE_FLAG_RE = re.compile(r"^\s+(--[A-Za-z][A-Za-z0-9-]*)\s\s+(.*)$")

# Cached for the life of the process, with a TTL so a pipeline update is picked up
# without restarting the bot.
_MODE_CACHE: dict = {"at": 0.0, "modes": None}
_MODE_TTL_SECONDS = 300


def discover_modes() -> Optional[dict]:
    """Ask the pipeline which template modes it has. ``None`` if it cannot be asked.

    Parses ``--help`` rather than importing the package, because the pipeline lives
    in its own virtualenv — the bot cannot import it, and shelling out to the entry
    point is exactly how it will be invoked anyway.
    """
    from . import jobs

    bin_name = config.JOBS_PIPELINE_BIN
    try:
        proc = subprocess.run([bin_name, "--help"], capture_output=True, text=True,
                              timeout=30, check=False, env=jobs.submit_env())
    except (OSError, subprocess.SubprocessError) as exc:
        log.debug("staging: could not ask %s for its modes (%s)", bin_name, exc)
        return None
    if proc.returncode != 0 or not proc.stdout:
        return None

    found = {}
    for line in proc.stdout.splitlines():
        match = _MODE_FLAG_RE.match(line)
        if not match:
            continue
        flag, description = match.group(1), match.group(2).lower()
        if _MODE_HELP_MARKER in description:
            name = _CURATED_NAMES.get(flag) or flag.lstrip("-").lower()
            found[name] = flag
    if not found:
        return None
    # The default (no flag) always exists; the pipeline cannot advertise it.
    found[_CURATED_NAMES[""]] = ""
    return found


def available_modes() -> dict:
    """``{name: flag}`` for the modes that exist right now.

    Falls back to the curated names when the pipeline cannot be reached, so tests
    and an offline deployment still work — with the caveat that a fallback list can
    be stale, which is why the drift is logged when it is visible.
    """
    now = time.monotonic()
    cached = _MODE_CACHE.get("modes")
    if cached is not None and now - _MODE_CACHE["at"] < _MODE_TTL_SECONDS:
        return cached

    discovered = discover_modes()
    if discovered is None:
        modes = {name: flag for flag, name in _CURATED_NAMES.items()}
    else:
        modes = discovered
        curated = {name for flag, name in _CURATED_NAMES.items()}
        added, gone = set(modes) - curated, curated - set(modes)
        if added:
            log.info("staging: pipeline offers new template mode(s): %s",
                     ", ".join(sorted(added)))
        if gone:
            log.info("staging: pipeline no longer offers: %s", ", ".join(sorted(gone)))

    _MODE_CACHE.update(at=now, modes=modes)
    return modes


def invalidate_modes() -> None:
    """Drop the cache — used by tests, and after a pipeline upgrade."""
    _MODE_CACHE.update(at=0.0, modes=None)


# Kept as a name for the curated fallback, since callers and tests refer to it.
TEMPLATE_MODES = {name: flag for flag, name in _CURATED_NAMES.items()}

MAX_XYZ_BYTES = 5 * 1024 * 1024     # a structure file is kilobytes; this is slack


class StagingError(jobs.JobsError):
    """Staging refused the request. User-facing message."""


def mode_flag(template_mode: str) -> str:
    """Map a mode name to its pipeline flag, or refuse.

    Validated against what the pipeline currently offers, not against a copy — so a
    mode added upstream works immediately, and the refusal below never again tells a
    user that something they just built does not exist.

    Still closed: the returned flag comes from the discovered map, never from the
    string the model passed, so nothing in a conversation can reach the argv.
    """
    modes = available_modes()
    key = (template_mode or "ca-fixed").strip().lower()
    if key not in modes:
        raise StagingError(
            f"Unknown template mode {template_mode!r}. The pipeline currently offers: "
            + ", ".join(sorted(modes))
        )
    return modes[key]


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def collect_structures(rel: str, owner: str, viewer_uid: str) -> tuple[list, dict]:
    """Resolve ``rel`` through the ordinary root fence and list its ``.xyz`` files.

    Goes through ``roots.resolve`` rather than touching the filesystem directly —
    the same seam every other path-taking tool uses. That is deliberate: the demo
    escapes in this codebase were both a code path that read the roster directly
    instead of going through the fence, and a submission tool that resolved its own
    paths would be the next one.
    """
    path, scope, error = roots.resolve(rel, owner, viewer_uid)
    if error:
        raise StagingError(error)
    if not path.exists():
        raise StagingError(f"'{rel}' does not exist.")

    if path.is_file():
        if path.suffix.lower() != ".xyz":
            raise StagingError(f"'{rel}' is not an .xyz structure file.")
        files = [path]
    else:
        files = sorted(p for p in path.iterdir() if p.is_file() and p.suffix.lower() == ".xyz")
    if not files:
        raise StagingError(f"No .xyz structure files found in '{rel}'.")

    for f in files:
        # A symlink in the *source* tree is fine to read, but it must not escape
        # the root — resolve() already fenced the directory, not each entry.
        if not roots.relative_to_scope(f.resolve(), scope):
            raise StagingError(f"'{f.name}' resolves outside {scope['name']}'s files.")
        if f.stat().st_size > MAX_XYZ_BYTES:
            raise StagingError(f"'{f.name}' is unexpectedly large for a structure file.")
    return files, scope


def stage(*, requester_uid: str, thread_ts: str, structures: list, scope: dict,
          template_mode: str, source_rel: str) -> Path:
    """Copy ``structures`` into this user's staging directory and record provenance.

    The destination comes from ``jobs.staging_dir_for`` — derived from the registry
    and the Slack event, never from tool input, so no wording can redirect a stage
    into another user's area (the ``write_workflow`` rule, C9).
    """
    dest = jobs.staging_dir_for(requester_uid, thread_ts)
    if dest.exists():
        # One staging dir per thread; a re-submission in the same thread gets a
        # fresh numbered sibling rather than silently mixing two batches' inputs.
        n = 2
        while (sib := dest.parent / f"{dest.name}-{n}").exists():
            n += 1
        dest = sib
    dest.mkdir(parents=True, exist_ok=False)
    dest.chmod(0o700)

    provenance = {
        "requested_by": requester_uid,
        "thread_ts": thread_ts,
        "source_scope": scope.get("name", ""),
        "source_path": source_rel,
        "template_mode": template_mode,
        "staged_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "structures": [],
    }
    for src in structures:
        target = dest / src.name
        # copy, never symlink: a symlink here would let anything that can write
        # in staging reach back into the calculations root through it.
        shutil.copyfile(src, target)
        target.chmod(0o600)
        provenance["structures"].append({
            "name": src.name,
            "source": str(src),
            "sha256": _sha256(target),
            "bytes": target.stat().st_size,
        })

    (dest / "provenance.json").write_text(json.dumps(provenance, indent=2))
    log.info("jobs: staged %d structure(s) for %s at %s",
             len(structures), requester_uid, dest)
    return dest


def new_run_dir(requester_uid: str, thread_ts: str):
    """A fresh staging directory for one run, in the requester's own area.

    Shared by the batch path (which copies structures into it) and the direct path
    (which writes one input and one script). Derived from the registry and the
    Slack event, never from tool input.
    """
    dest = jobs.staging_dir_for(requester_uid, thread_ts)
    if dest.exists():
        n = 2
        while (sib := dest.parent / f"{dest.name}-{n}").exists():
            n += 1
        dest = sib
    dest.mkdir(parents=True, exist_ok=False)
    dest.chmod(0o700)
    return dest


def parse_submitted_jobs(stdout: str, staging_dir: Path) -> list:
    """Recover the job IDs the pipeline reported, for the ledger.

    The pipeline prints its own submission lines and also writes ``batch-jobs.log``
    in the output root; the log is the authoritative record, so it is preferred and
    stdout is the fallback. Anything we cannot attribute is still recorded with the
    kind left blank rather than dropped — an unattributed row is still cancellable,
    which is the property that matters.
    """
    found: dict = {}

    log_path = staging_dir / "batch-jobs.log"
    if log_path.is_file():
        try:
            for line in log_path.read_text(errors="replace").splitlines():
                parts = [p.strip() for p in line.split("\t")]
                ids = [p for p in parts if p.isdigit()]
                if not ids:
                    continue
                name = next((p for p in parts if p and not p.isdigit()), "")
                found[ids[0]] = {"job_id": ids[0], "kind": _kind_of(name),
                                 "job_name": name, "work_dir": str(staging_dir)}
        except OSError:
            log.warning("jobs: could not read %s", log_path, exc_info=True)

    for line in (stdout or "").splitlines():
        # "Submitted batch job 12345"
        parts = line.split()
        if "Submitted" in parts and parts[-1].isdigit():
            jid = parts[-1]
            found.setdefault(jid, {"job_id": jid, "kind": _kind_of(line),
                                   "job_name": "", "work_dir": str(staging_dir)})
    return list(found.values())


def _kind_of(text: str) -> str:
    low = (text or "").lower()
    if "postproc" in low:
        return "postprocess"
    if "corvus" in low or "feff" in low:
        return "corvus"
    if "orca" in low:
        return "orca"
    return ""
