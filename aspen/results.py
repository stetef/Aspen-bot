"""
Reading back what a job actually produced.

A submitted batch does not write into anybody's calculations tree — that is the
invariant staging exists to protect (spec §19.3): Aspen copies structures *out* of
a root and everything the run produces lands under ``JOBS_STAGING_ROOT`` instead.
Which left a gap nobody noticed until the first real submission finished. The
outputs were the point of the run, and Aspen could not read them: every read tool
goes through ``roots.resolve``, staging is not a root, and ``roots.validate``
explicitly refuses to let it become one (a root overlapping ``WORKSPACE_ROOT``
would put the agent's own state inside a readable tree). So Aspen would announce a
finished batch and then, asked what the ORCA output said, have to answer that it
could not open it.

This module closes that gap **without** touching the fence that produced it. Three
things make that safe, and all three are properties staging already had:

* **Staging is results, not state.** It is deliberately world-readable (0755, see
  ``config.JOBS_STAGING_MODE``) so colleagues can copy runs out, and the analysis
  jail bind-mounts only ``figures/`` and ``cache/`` read-write — so nothing the
  agent can *run* can write here. Reading it back is not reading anything the
  model could have planted.
* **The model names a batch, never a path.** Exactly the rule that makes
  ``@alias`` roots unspoofable (``roots``, C9): a batch ID is looked up in the
  ledger, and the directory comes from the row Aspen wrote at submit time —
  derived from the registry and the Slack event. A model can be argued into
  passing any value it is handed; it cannot pass one it never received.
* **It is read-only, and it stays read-only.** There is no write, no copy-into, and
  deliberately no way to feed a staged file back in as a job input. When a user
  wants results in their own tree, Aspen hands them the ``cp`` line
  (:func:`copy_command`) and they run it as themselves — which keeps "Aspen writes
  nothing inside a calculations root" literally true rather than nearly true.

Reads are **flat**, like every other read in this codebase: any registered user may
read any batch's results, because they may already do so on the shared filesystem
and a permission model here would only be a wrong copy of the real one. What the
batch row does decide is *attribution* — whose run this was — which is why every
path handed back says so.
"""

import logging
import re
from pathlib import Path
from typing import Optional

from . import config, demo, jobs, roots

log = logging.getLogger("aspen")

# ``uuid4().hex[:16]``, and nothing else. Checked before the ledger is touched so
# that a value the model made up is refused as malformed rather than as missing —
# and so no string that looks like a path can ever reach a lookup.
_BATCH_ID_RE = re.compile(r"^[0-9a-f]{8,32}$")

# Marks a results path on output: ``batch:4f3a…/orca/h2o.out``. Distinct from the
# ``@alias`` prefix on purpose — these are two different namespaces and a reader
# should never have to wonder which one they are looking at.
PREFIX = "batch:"


def batch_row(batch_id: str) -> Optional[dict]:
    """The ledger row for ``batch_id``, or None. Never raises on a bad ID."""
    if not _BATCH_ID_RE.match((batch_id or "").strip().lower()):
        return None
    return jobs.batch_by_id(batch_id.strip().lower())


def _refuse_unsafe_base(base: Path) -> str:
    """Refuse a recorded staging directory that is not somewhere results live.

    The row is trustworthy by construction — Aspen derived the path from the
    registry and the Slack event and wrote it itself — so this is defense in depth
    rather than validation, and it is deliberately the *narrow* rule: the directory
    must be inside the configured staging root. Reading is confined to one tree
    that way, with no case analysis about what else a path might have been.

    The cost is that relocating ``ASPEN_JOBS_STAGING_ROOT`` makes older batches
    unreadable *through Aspen* (the cancel fence keeps its own per-batch rule, so
    running jobs are unaffected, and the files themselves are still there to copy).
    That is the right way round: a fence that is obviously correct, and a
    degradation that costs a `cp`.

    The sandbox check underneath is what the startup guard enforces globally
    (main._check_state_locations). Repeated here because it is the one that matters
    — anything the jail can write is content the model could have planted, and this
    module's whole safety argument is that staging is not that.
    """
    if not roots._under(base, config.JOBS_STAGING_ROOT):
        return ("Error: that batch's directory is not inside Aspen's staging area; "
                "refusing to read it.")
    jail_writable = [config.WORKSPACE_ROOT / "figures", config.WORKSPACE_ROOT / "cache"]
    for area in [Path(p).expanduser() for p in config.SANDBOX_WRITE_PATHS] + jail_writable:
        if roots._under(base, area):
            return ("Error: that batch's directory is inside a path the analysis "
                    "sandbox can write; refusing to read it.")
    return ""


def resolve(batch_id: str, rel: str, viewer_uid: str) -> tuple[Optional[Path], dict, str]:
    """Resolve ``rel`` inside a batch's results. ``(path, scope, error)``.

    Mirrors :func:`roots.resolve` deliberately, down to the return shape, so the
    tools can treat the two fences as one seam with two implementations rather than
    growing a second way of thinking about paths.

    The containment check happens *after* symlinks are followed. That matters more
    here than in a calculations root: a job runs unjailed on a compute node, so a
    run could in principle leave a symlink in its own output directory pointing
    anywhere the bot's account can read. ``resolve()`` plus ``relative_to`` refuses
    to follow it out.
    """
    scope = {"name": "", "kind": "results", "path": None, "batch_id": "", "owner_id": ""}

    # A demo visitor gets the demo tree and nothing else — the one place reads are
    # restricted by identity, and it restricts them to strictly less than a
    # registered user sees. Job results are real users' work; they are not in it.
    if demo.active_for(viewer_uid) is not None:
        return None, scope, ("Error: this is a demo session, so submitted-job results "
                             "aren't reachable from here.")

    row = batch_row(batch_id)
    if row is None:
        return None, scope, (
            f"Error: no batch '{batch_id}' in the job ledger. Batch IDs come from "
            "list_my_jobs, from the reply that confirmed the submission, or from the "
            "message saying the batch had finished."
        )

    base = Path(row["staging_dir"]).resolve()
    scope.update(name=row.get("alias") or "", path=base, batch_id=row["batch_id"],
                 owner_id=row.get("slack_user_id") or "")

    unsafe = _refuse_unsafe_base(base)
    if unsafe:
        log.warning("results: refusing batch %s at %s", row["batch_id"], base)
        return None, scope, unsafe
    if not base.is_dir():
        return None, scope, (
            f"Error: batch {row['batch_id']} has no results directory on disk any more "
            f"({base}). It may have been cleaned up."
        )

    bare = (rel or ".").strip()
    while bare.startswith("./"):
        bare = bare[2:]
    try:
        resolved = (base / (bare or ".")).resolve()
        resolved.relative_to(base)              # raises ValueError if it escapes
    except (ValueError, OSError):
        return None, scope, (
            f"Error: '{bare}' is outside batch {row['batch_id']}'s results directory."
        )
    return resolved, scope, ""


def qualify(scope: dict, rel: str = ".") -> str:
    """Display form: ``batch:<id>/rel`` — a path that says which run it came from."""
    rel = (rel or ".").strip()
    while rel.startswith("./"):             # not lstrip("./"), which eats a leading dot
        rel = rel[2:]
    rel = rel or "."
    head = f"{PREFIX}{scope.get('batch_id', '')}"
    return head if rel == "." else f"{head}/{rel}"


def relative_to_scope(path: Path, scope: dict) -> str:
    """A resolved path back in display form, for echoing results."""
    try:
        return qualify(scope, str(Path(path).relative_to(scope["path"])))
    except (ValueError, KeyError, TypeError):
        return str(path)


def copy_command(row: dict, viewer_uid: str = "") -> str:
    """The ``cp`` line that puts a run's results in the reader's own tree.

    A command rather than a tool on purpose. Aspen writing into a calculations root
    is the one thing it never does, and "never" is worth more than the convenience
    of not typing this — so the user runs it, as themselves, with their own
    permissions and their own idea of where it belongs.
    """
    source = str(row.get("staging_dir") or "").rstrip("/")
    if not source:
        return ""
    if roots.is_rootless(viewer_uid):
        return f"cp -r {source} <somewhere you keep results>/"
    destination = Path(roots.for_user(viewer_uid) if viewer_uid
                       else config.CALCULATIONS_ROOT)
    # Into the project the batch came from, when it had one — the run's results
    # belong beside the structures it ran on, not loose at the top of the tree.
    project = (row.get("project") or "").strip()
    if project:
        destination = destination / project
    return f"cp -r {source} {destination}/"
