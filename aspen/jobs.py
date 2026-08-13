"""
Slurm job submission and cancellation — the ledger, the argv, and who may cancel what.

This is the only capability that spends a **shared** resource, and during the beta
it runs under the developer's own Unix account (spec §19). That account model
changes what the code here is load-bearing *for*, and the distinction is worth
stating once at the top because it is the reason for nearly every check below.

Under a dedicated ``aspen-agent`` service account, Unix and Slurm ownership are a
free outer layer: the account simply cannot ``scancel`` anyone else's job, and
everything in this module is defense in depth. Running as the developer removes
that layer **and inverts it** — every job the developer ever submitted by hand is
inside ``scancel`` range of the credentials the bot holds. So the three checks in
:func:`resolve_cancellable` are not redundancy. They are the only thing between a
confused or injected model and somebody's queue.

The tag that replaces Unix ownership is ``WorkDir``, which Slurm records itself.
Aspen cannot set ``--comment``: the pipeline invokes ``sbatch`` on its own, and
Slurm offers no ``SBATCH_COMMENT`` environment variable to inject one through
(``--wckey`` is dropped too — s3df reports ``TrackWCKey = no``). What Slurm does
record unasked is the submission directory, and the pipeline's scripts run with
``--chdir=.`` from their staging run directory. That makes ``WorkDir``:

* set by Slurm rather than by Aspen, so conversation text cannot forge it;
* per-user by construction, since staging is ``<root>/<alias>__<slack-id>/<thread>/``,
  which reuses the path fence used everywhere else instead of inventing a second
  notion of ownership;
* structurally absent from hand-submitted jobs — a job launched from a personal
  tree *cannot* have a ``WorkDir`` under the staging root, so it fails the check by
  construction rather than by policy. This is what restores the protection that
  Unix ownership would otherwise give.

Job IDs are reused (s3df: ``MaxJobId = 67043328``, and the counter resets on a
controller rebuild), which is why verification runs against live Slurm state at
cancel time instead of trusting the ledger alone.
"""

import json
import logging
import os
import re
import shutil
import sqlite3
import subprocess
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, Optional

from . import config, registry

log = logging.getLogger("aspen")

# Terminal Slurm states — a job in one of these cannot be cancelled and is not
# counted against the concurrency caps.
TERMINAL_STATES = frozenset({
    "COMPLETED", "FAILED", "CANCELLED", "TIMEOUT", "NODE_FAIL",
    "PREEMPTED", "BOOT_FAIL", "DEADLINE", "OUT_OF_MEMORY", "REVOKED",
    "SPECIAL_EXIT",
})

# Job kinds the pipeline creates, in dependency order.
JOB_KINDS = ("orca", "corvus", "postprocess")

# ``scancel`` flags that select jobs by predicate rather than by ID. NEVER built.
#
# Every one of them delegates enumeration to Slurm, which contradicts the rule
# that the ledger is the only source of candidate IDs — and it makes per-job
# verification impossible, because you cannot check the WorkDir of a job you never
# enumerated. ``scancel -u <user>`` is one word away from the entire queue.
# A contract test asserts none of these appears in a built argv.
FORBIDDEN_SCANCEL_FLAGS = frozenset({
    "-u", "--user", "-n", "--name", "--jobname", "-A", "--account",
    "-p", "--partition", "-q", "--qos", "-t", "--state", "--me",
    "-w", "--nodelist", "-R", "--reservation", "--wckey", "--cron",
})

_JOB_ID_RE = re.compile(r"^\d+(_\d+)?$")          # 12345 or 12345_7 (array task)
_SLACK_ID_RE = re.compile(r"^[A-Z0-9]+$")


class JobsError(Exception):
    """A submission or cancellation was refused. The message is user-facing."""


# --------------------------------------------------------------------------- #
# Ledger
# --------------------------------------------------------------------------- #
_SCHEMA = """
CREATE TABLE IF NOT EXISTS batches (
    batch_id      TEXT PRIMARY KEY,
    slack_user_id TEXT NOT NULL,
    alias         TEXT NOT NULL,
    thread_ts     TEXT NOT NULL,
    project       TEXT NOT NULL,
    owner_scope   TEXT NOT NULL,
    template_mode TEXT NOT NULL,
    staging_dir   TEXT NOT NULL,
    structures    INTEGER NOT NULL,
    argv          TEXT NOT NULL,
    submitted_at  TEXT NOT NULL,
    -- Where to post when this batch finishes, and whether to. The channel is
    -- stored because a thread_ts alone is not addressable.
    channel       TEXT,
    notify        INTEGER DEFAULT 0,
    notified_at   TEXT
);
CREATE TABLE IF NOT EXISTS jobs (
    job_id        TEXT NOT NULL,
    batch_id      TEXT NOT NULL,
    kind          TEXT NOT NULL,
    job_name      TEXT,
    work_dir      TEXT NOT NULL,
    submitted_at  TEXT NOT NULL,
    -- Reconciler-owned columns; everything above is immutable.
    state         TEXT,
    elapsed       TEXT,
    total_cpu     TEXT,
    alloc_tres    TEXT,
    exit_code     TEXT,
    reconciled_at TEXT,
    cancelled_at  TEXT,
    -- The --comment tag, when Aspen submitted this job itself (direct runners).
    -- Empty for pipeline runners, which call sbatch on their own and give Aspen no
    -- way to set one. A row that HAS a tag must match it at cancel time.
    comment       TEXT,
    PRIMARY KEY (job_id, batch_id)
);
CREATE INDEX IF NOT EXISTS idx_batches_user ON batches(slack_user_id);
CREATE INDEX IF NOT EXISTS idx_batches_at   ON batches(submitted_at);
CREATE INDEX IF NOT EXISTS idx_jobs_batch   ON jobs(batch_id);
CREATE INDEX IF NOT EXISTS idx_jobs_state   ON jobs(state);
"""


def connect() -> sqlite3.Connection:
    """Open the ledger, creating it (and its parent) on first use.

    No WAL: the ledger sits in ``STATE_DIR``, which on this deployment is a
    parallel filesystem where WAL's ``-shm`` mmap is unreliable (spec §12). A
    ``busy_timeout`` covers the bot and the CLI touching it at once.
    """
    path = config.JOBS_LEDGER
    registry.ensure_private_dir(path.parent)
    conn = sqlite3.connect(str(path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(_SCHEMA)
    _migrate(conn)
    conn.commit()
    return conn


def _migrate(conn) -> None:
    """Add columns a previous version did not have.

    ``CREATE TABLE IF NOT EXISTS`` silently does nothing to an existing table, so a
    ledger written before a column existed keeps working but loses the new field —
    which for ``comment`` would mean a tagged job reading as untagged, i.e. one
    fewer check at cancel time. Cheap to do, expensive to discover.
    """
    have = {row[1] for row in conn.execute("PRAGMA table_info(jobs)")}
    for column, decl in (("comment", "TEXT"),):
        if column not in have:
            conn.execute(f"ALTER TABLE jobs ADD COLUMN {column} {decl}")
            log.info("jobs: added ledger column jobs.%s", column)

    have = {row[1] for row in conn.execute("PRAGMA table_info(batches)")}
    for column, decl in (("channel", "TEXT"), ("notify", "INTEGER DEFAULT 0"),
                         ("notified_at", "TEXT")):
        if column not in have:
            conn.execute(f"ALTER TABLE batches ADD COLUMN {column} {decl}")
            log.info("jobs: added ledger column batches.%s", column)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def record_batch(*, slack_user_id: str, alias: str, thread_ts: str, project: str,
                 owner_scope: str, template_mode: str, staging_dir: Path,
                 structures: int, argv: list, channel: str = "",
                 notify: bool = False) -> str:
    """Write the batch row and return its id. Called BEFORE the pipeline runs.

    Spec §18.2's "every submission is fully logged before sbatch" requirement,
    kept literally: this raises on failure, and the caller lets that abort the
    submission. A job running with no ledger row is a job nobody can cancel
    through Aspen and nobody can attribute — strictly worse than not submitting.
    """
    batch_id = uuid.uuid4().hex[:16]
    with connect() as conn:
        conn.execute(
            "INSERT INTO batches (batch_id, slack_user_id, alias, thread_ts, "
            "project, owner_scope, template_mode, staging_dir, structures, argv, "
            "submitted_at, channel, notify) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (batch_id, slack_user_id, alias, thread_ts, project, owner_scope,
             template_mode, str(staging_dir), int(structures),
             json.dumps(list(argv)), _utc_now(), channel, 1 if notify else 0),
        )
    return batch_id


def record_jobs(batch_id: str, entries: Iterable[dict]) -> int:
    """Record the scheduler jobs a batch produced. Immutable once written."""
    rows = [
        (str(e["job_id"]), batch_id, e.get("kind", ""), e.get("job_name", ""),
         str(e["work_dir"]), _utc_now(), e.get("comment", ""))
        for e in entries
    ]
    if not rows:
        return 0
    with connect() as conn:
        conn.executemany(
            "INSERT OR IGNORE INTO jobs (job_id, batch_id, kind, job_name, "
            "work_dir, submitted_at, comment) VALUES (?,?,?,?,?,?,?)",
            rows,
        )
    return len(rows)


def batches_for(slack_user_id: str, limit: int = 20) -> list:
    with connect() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM batches WHERE slack_user_id = ? "
            "ORDER BY submitted_at DESC LIMIT ?", (slack_user_id, limit))]


def jobs_for_batch(batch_id: str) -> list:
    with connect() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM jobs WHERE batch_id = ? ORDER BY submitted_at, kind",
            (batch_id,))]


def reconcile_quietly(days: int = 30) -> None:
    """Refresh job states without letting a bad sacct call break the caller."""
    try:
        reconcile(days=days)
    except Exception:
        log.debug("jobs: background reconcile failed", exc_info=True)


def _backfill_quietly() -> None:
    """Best-effort recovery on any ledger read. Never raises into a caller."""
    try:
        backfill()
    except Exception:
        log.debug("jobs: backfill pass failed", exc_info=True)


def active_rows(slack_user_id: str = "") -> list:
    """Ledger rows not known to be terminal, joined to their batch.

    ``state IS NULL`` counts as active: the reconciler may not have run yet, and
    treating unknown as finished would let the caps be walked past by never
    reconciling.
    """
    _backfill_quietly()
    sql = (
        "SELECT j.*, b.slack_user_id, b.alias, b.project, b.thread_ts, "
        "       b.staging_dir, b.template_mode "
        "FROM jobs j JOIN batches b ON b.batch_id = j.batch_id "
        "WHERE j.cancelled_at IS NULL "
        "  AND (j.state IS NULL OR j.state NOT IN (%s))"
        % ",".join("?" * len(TERMINAL_STATES))
    )
    params = list(TERMINAL_STATES)
    if slack_user_id:
        sql += " AND b.slack_user_id = ?"
        params.append(slack_user_id)
    with connect() as conn:
        return [dict(r) for r in conn.execute(sql, params)]


def submits_today(slack_user_id: str) -> int:
    since = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat(timespec="seconds")
    with connect() as conn:
        (n,) = conn.execute(
            "SELECT COUNT(*) FROM batches WHERE slack_user_id = ? AND submitted_at >= ?",
            (slack_user_id, since),
        ).fetchone()
    return int(n)


def mark_cancelled(job_ids: Iterable[str]) -> None:
    ids = [str(j) for j in job_ids]
    if not ids:
        return
    with connect() as conn:
        conn.executemany(
            "UPDATE jobs SET cancelled_at = ? WHERE job_id = ?",
            [(_utc_now(), j) for j in ids],
        )


def apply_reconciliation(rows: Iterable[dict]) -> int:
    """Fill in the reconciler-owned columns. The only UPDATE of a submit row's data."""
    updates = [
        (r.get("state"), r.get("elapsed"), r.get("total_cpu"), r.get("alloc_tres"),
         r.get("exit_code"), _utc_now(), str(r["job_id"]))
        for r in rows
    ]
    if not updates:
        return 0
    with connect() as conn:
        conn.executemany(
            "UPDATE jobs SET state=?, elapsed=?, total_cpu=?, alloc_tres=?, "
            "exit_code=?, reconciled_at=? WHERE job_id=?",
            updates,
        )
    return len(updates)


# --------------------------------------------------------------------------- #
# Staging paths — the fence that WorkDir verification checks against
# --------------------------------------------------------------------------- #
def staging_dir_for(slack_user_id: str, thread_ts: str) -> Path:
    """``<staging root>/<alias>__<slack-id>/<thread-ts>``.

    Derived entirely from the registry and the Slack event — never from tool
    input. The ``alias__slack-id`` shape mirrors ``workflows.dir_for`` and
    ``metadata``: found by ID, so a rename cannot orphan anyone's jobs, and
    readable by a human scanning the directory.
    """
    if not _SLACK_ID_RE.match(slack_user_id or ""):
        raise JobsError("Internal error: refusing to stage without a Slack user ID.")
    user = registry.by_id(slack_user_id) or {}
    alias = user.get("alias") or "unknown"
    # thread_ts is a Slack timestamp ("1723480000.123456"); keep it filesystem-safe
    # without losing identity. Slack never puts a separator in it, but a thread_ts
    # is still event data, so it is sanitised rather than trusted.
    safe_thread = re.sub(r"[^0-9.]", "", str(thread_ts)) or "nothread"
    return config.JOBS_STAGING_ROOT / f"{alias}__{slack_user_id}" / safe_thread


def user_staging_root(slack_user_id: str) -> Path:
    """The per-user staging directory — the fence a job's WorkDir must sit inside."""
    if not _SLACK_ID_RE.match(slack_user_id or ""):
        raise JobsError("Internal error: refusing to resolve staging without a Slack ID.")
    user = registry.by_id(slack_user_id) or {}
    alias = user.get("alias") or "unknown"
    return config.JOBS_STAGING_ROOT / f"{alias}__{slack_user_id}"


def require_registered(slack_user_id: str) -> dict:
    """The requester must be an active registry user. Returns their record.

    The tool surface already withholds the job tools from a demo session
    (``tools.active_specs``), and admission already gates every turn — so this is
    the third check on the same thing, which is deliberate. Withholding is session
    state, admission is per-turn, and this is per-action; the demo escapes recorded
    in THREAT_MODEL §3 were both a path that skipped the fence its neighbours went
    through. Compute is the one asset a mistake here spends irrecoverably, so it
    verifies rather than assuming an earlier gate held.
    """
    if not _SLACK_ID_RE.match(slack_user_id or ""):
        raise JobsError("Internal error: no Slack user ID on this request.")
    user = registry.by_id(slack_user_id, include_removed=False)
    if not user or user.get("status") != "active":
        raise JobsError(
            "Job submission is only available to registered Aspen users. "
            "Ask an admin to add you with `aspen-users add`."
        )
    if not user.get("alias"):
        raise JobsError("Internal error: your registry entry has no alias.")
    return user


def _within(path: Path, parent: Path) -> bool:
    """True if ``path`` is inside ``parent``, symlinks resolved.

    ``resolve()`` on both sides is the point: a symlink inside staging pointing
    at someone else's tree must not read as containment.
    """
    try:
        return path.resolve().is_relative_to(parent.resolve())
    except (OSError, ValueError):
        return False


# --------------------------------------------------------------------------- #
# Environment for the orchestrator subprocess (and therefore for every job)
# --------------------------------------------------------------------------- #
def submit_env() -> dict:
    """The scrubbed environment the pipeline subprocess runs with.

    ``config.load_dotenv()`` puts ``SLACK_BOT_TOKEN``, ``SLACK_APP_TOKEN`` and
    ``AGENT_INTERNAL_SECRET`` into this process's ``os.environ``, and ``sbatch``
    defaults to ``--export=ALL`` — so without scrubbing, every compute node in the
    batch would receive Aspen's Slack tokens.

    An **allowlist**, deliberately. A denylist of secret names silently fails to
    cover the next secret someone adds to ``.env``, and the failure is invisible
    until it is a credential on a shared cluster.

    Not ``--export=NONE``, which looks like the stronger move: the pipeline's ORCA
    script triages its own failures by calling ``xas-rerun-orca``, found on an
    inherited ``PATH`` behind a ``command -v`` guard. Under ``--export=NONE`` that
    guard fails and auto-rerun becomes a *silent* no-op. Scrubbing the parent
    environment achieves the same security result with no invisible cost.
    """
    allowed = set(config.JOBS_ENV_BASE) | set(config.JOBS_ENV_PASSTHROUGH)
    env = {}
    for key, value in os.environ.items():
        keep = key in allowed or key.startswith(tuple(config.JOBS_ENV_PREFIXES))
        if keep:
            env[key] = value

    # Belt and braces: nothing on the forbidden list survives, whatever an
    # operator put in ASPEN_JOBS_ENV_PASSTHROUGH or however a prefix matched.
    for name in config.JOBS_ENV_FORBIDDEN:
        env.pop(name, None)
    # And nothing that merely *looks* like a secret, for the names we cannot
    # enumerate ahead of time.
    for key in list(env):
        if re.search(r"(TOKEN|SECRET|PASSWORD|APIKEY|API_KEY|CREDENTIAL)", key, re.I):
            env.pop(key, None)

    # Put the pipeline on the PATH the job will actually run with. subprocess
    # resolves an unqualified program name against the PATH in the env it is
    # handed (verified, not assumed), so this both finds the orchestrator and
    # gives the compute-node script the PATH its auto-rerun triage needs.
    bin_dir = config.JOBS_PIPELINE_PATH_DIR
    if bin_dir:
        env["PATH"] = bin_dir + os.pathsep + env.get("PATH", "")

    # Defense in depth beside the scrub: name the exported set explicitly, so a
    # future sbatch default change cannot widen it.
    env["SBATCH_EXPORT"] = config.JOBS_SBATCH_EXPORT or ",".join(sorted(env))
    return env


# --------------------------------------------------------------------------- #
# argv builders — pure functions, so the tests need no cluster
# --------------------------------------------------------------------------- #
def build_submit_argv(*, staging_dir: Path, out_dir: Path, template_mode: str,
                      dry_run: bool) -> list:
    """The pipeline invocation. A list, never a shell string.

    Note what the model contributes: a ``template_mode`` that has already been
    validated against a fixed enum, and paths this module derived. It supplies no
    script, no flags, and no free text. There is nothing here for conversation to
    steer, which is why this can be a pure function with an exhaustive test.
    """
    from . import staging  # local import: staging imports jobs for the enum

    flag = staging.mode_flag(template_mode)   # raises on anything off the enum
    argv = [
        config.JOBS_PIPELINE_BIN,
        str(staging_dir),
        "--scheduler", config.JOBS_SCHEDULER,
        "--out-dir", str(out_dir),
    ]
    if flag:
        argv.append(flag)
    if dry_run:
        argv.append("--no-submit")
    return argv


def build_scancel_argv(job_ids: Iterable[str]) -> list:
    """``scancel <id> <id> …`` — explicit IDs only, no predicates.

    Every ID is re-validated here even though it came from the ledger, because
    this function is the last thing between a string and a process. Filter flags
    are not merely unused: passing one is a programming error worth crashing on,
    since it would silently widen what a cancel touches.
    """
    ids = [str(j) for j in job_ids]
    if not ids:
        raise JobsError("Nothing to cancel.")
    for jid in ids:
        if jid in FORBIDDEN_SCANCEL_FLAGS or jid.startswith("-"):
            raise JobsError(
                f"Refusing to build a scancel with the option {jid!r}: cancellation "
                "is by explicit job ID only."
            )
        if not _JOB_ID_RE.match(jid):
            raise JobsError(f"Refusing to cancel {jid!r}: not a Slurm job ID.")
    return ["scancel", *ids]


def build_sacct_argv(*, user: str, since: str) -> list:
    """The reconciler's read. ``-X`` avoids double-counting ``.batch``/``.extern`` steps."""
    return [
        "sacct", "-X", "-n", "-P", "-u", user, "-S", since,
        "-o", "JobID,JobName,Comment,State,Submit,Start,End,Elapsed,"
              "TotalCPU,AllocTRES,ExitCode",
    ]


# --------------------------------------------------------------------------- #
# Live Slurm state
# --------------------------------------------------------------------------- #
def _run_slurm(argv: list) -> subprocess.CompletedProcess:
    return subprocess.run(
        argv, capture_output=True, text=True, check=False,
        timeout=config.JOBS_SLURM_TIMEOUT, env=submit_env(),
    )


def scontrol_job(job_id: str) -> Optional[dict]:
    """Parse ``scontrol show job <id> -o`` into a dict, or None if unavailable.

    Returns None — never a partial dict — when Slurm cannot tell us about the job,
    so callers cannot mistake "no information" for "information that passed".
    """
    if not _JOB_ID_RE.match(str(job_id)):
        return None
    try:
        proc = _run_slurm(["scontrol", "show", "job", str(job_id), "-o"])
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("jobs: scontrol failed for %s: %s", job_id, exc)
        return None
    if proc.returncode != 0 or not proc.stdout.strip():
        return None
    # "JobId=123 JobName=x WorkDir=/p UserId=me(1001) JobState=RUNNING ..."
    out = {}
    for token in proc.stdout.split():
        key, sep, value = token.partition("=")
        if sep:
            out.setdefault(key, value)
    return out or None


def whoami() -> str:
    """The Unix user Aspen runs as — the account whose jobs it may cancel."""
    from . import roots
    return roots._whoami()


# --------------------------------------------------------------------------- #
# THE ownership chokepoint
# --------------------------------------------------------------------------- #
def resolve_cancellable(requester_uid: str, selector: str = "") -> tuple[list, list]:
    """What ``requester_uid`` may cancel right now. The single authorization seam.

    Returns ``(approved, refused)`` — ``approved`` is a list of verified ledger
    rows, ``refused`` a list of ``(job_id, reason)`` pairs kept so the reply can
    say what was skipped instead of silently narrowing.

    Deliberately **one** implementation, not duplicated into the tool server: the
    threat model records that both scope escapes in this codebase were a second
    code path reading the registry directly instead of going through the fence
    (THREAT_MODEL §3). A cancel check that exists twice is that shape again.

    Three gates, each of which must pass, and each failing closed:

    1. **Ledger membership and ownership.** Candidates come only from rows whose
       ``slack_user_id`` equals ``requester_uid`` — which callers take from the
       Slack event, never from tool input (there is no ``owner`` parameter, by
       design). A ``selector`` may narrow the set; it can never widen it, because
       it is applied as a filter over rows already restricted to this user.
    2. **The job is ours to cancel.** ``scontrol`` must report the job, and its
       ``UserId`` must be the account Aspen runs as.
    3. **WorkDir inside this requester's staging tree.** This is what makes the
       beta account model survivable: the developer's own hand-submitted jobs
       cannot satisfy it, and neither can another Slack user's Aspen jobs. It also
       closes the recycled-ID window — a stale row pointing at a live unrelated
       job fails here even though gate 1 passed.
    """
    require_registered(requester_uid)

    rows = active_rows(requester_uid)
    if selector:
        rows = _apply_selector(rows, selector)

    fence = user_staging_root(requester_uid)
    me = whoami()
    approved, refused = [], []

    for row in rows:
        jid = str(row["job_id"])
        info = scontrol_job(jid)
        if info is None:
            refused.append((jid, "Slurm has no current record of it (already finished?)"))
            continue

        owner = info.get("UserId", "")
        # "tetef01(1234)" -> "tetef01"
        owner_name = owner.split("(")[0]
        if owner_name != me:
            refused.append((jid, f"it belongs to Unix user {owner_name or '?'}, not {me}"))
            continue

        work_dir = info.get("WorkDir", "")
        if not work_dir:
            refused.append((jid, "Slurm reports no WorkDir, so ownership can't be verified"))
            continue
        # Against the staging directory recorded for THIS batch as well as the
        # user's current one. The recorded path is Aspen-derived and was created
        # under that user's root at submit time, so containment in it is the same
        # guarantee — and it means relocating the staging root does not orphan jobs
        # that are already running from the old one.
        recorded = str(row.get("staging_dir") or "")
        allowed = [fence] + ([Path(recorded)] if recorded else [])
        if not any(_within(Path(work_dir), area) for area in allowed):
            # The important refusal: everything else is bookkeeping, this is the
            # one that stops a cancel crossing a person.
            refused.append((jid, "its working directory is outside your own staging area"))
            log.warning(
                "jobs: refused cancel of %s for %s — WorkDir %s outside %s",
                jid, requester_uid, work_dir, fence,
            )
            continue

        # The tag, where there is one. Direct runners get it because Aspen calls
        # sbatch itself; the pipeline path cannot set it (no SBATCH_COMMENT), so its
        # jobs are fenced by WorkDir alone.
        comment = info.get("Comment", "")
        expected = f"{COMMENT_PREFIX}/{requester_uid}/"
        recorded = (row.get("comment") or "").strip()

        if recorded:
            # The ledger says WE tagged this job. Slurm must agree, exactly. A
            # tagged row whose live comment is missing or different is either a
            # recycled ID or something stranger — either way, not ours to cancel.
            if comment != recorded:
                refused.append((jid, "its Aspen tag no longer matches our record"))
                log.warning("jobs: tag mismatch on %s: recorded %r, live %r",
                            jid, recorded, comment)
                continue
            if not comment.startswith(expected):
                refused.append((jid, "its Aspen tag names a different user"))
                log.warning("jobs: comment/user mismatch on %s: %r", jid, comment)
                continue
        elif comment.startswith(COMMENT_PREFIX + "/") and not comment.startswith(expected):
            # Untagged in the ledger but tagged in Slurm, for someone else.
            refused.append((jid, "its Aspen tag names a different user"))
            log.warning("jobs: comment mismatch on %s: %r", jid, comment)
            continue

        row = dict(row)
        row["live_state"] = info.get("JobState", "")
        row["live_work_dir"] = work_dir
        approved.append(row)

    return approved, refused


def _apply_selector(rows: list, selector: str) -> list:
    """Narrow ``rows`` by job ID, batch ID, or project name. Never widens.

    Applied *after* the per-user filter, so the worst a hostile selector achieves
    is selecting fewer of the requester's own jobs.
    """
    sel = selector.strip()
    if not sel or sel.lower() == "all":
        return rows
    hits = [r for r in rows if str(r["job_id"]) == sel]
    if hits:
        return hits
    hits = [r for r in rows if r["batch_id"] == sel]
    if hits:
        return hits
    low = sel.lower()
    hits = [r for r in rows if (r.get("project") or "").lower() == low]
    if hits:
        return hits
    return []


def cancel(requester_uid: str, selector: str = "") -> dict:
    """Verify, then ``scancel`` the approved IDs. Returns a report for the reply."""
    approved, refused = resolve_cancellable(requester_uid, selector)
    if not approved:
        return {"cancelled": [], "refused": refused, "ok": False}

    ids = [str(r["job_id"]) for r in approved]
    argv = build_scancel_argv(ids)
    try:
        proc = _run_slurm(argv)
    except (OSError, subprocess.SubprocessError) as exc:
        raise JobsError(f"Could not run scancel: {exc}") from exc

    if proc.returncode != 0:
        # scancel exits non-zero if *any* ID failed; it does not say which. The
        # ledger is not marked, so a retry re-verifies rather than assuming.
        raise JobsError(
            "scancel reported an error: "
            + (proc.stderr.strip() or f"exit {proc.returncode}")
        )

    mark_cancelled(ids)
    log.info("jobs: cancelled %s for %s", ",".join(ids), requester_uid)
    return {"cancelled": approved, "refused": refused, "ok": True}


# --------------------------------------------------------------------------- #
# Caps
# --------------------------------------------------------------------------- #
def check_caps(requester_uid: str, structures: int) -> None:
    """Raise :class:`JobsError` if this submission would exceed a cap.

    Python-enforced rather than described in the prompt: the primary threat actor
    is the careless allowlisted member and the asset is compute shared with the
    whole group, so "the model was asked not to" is not a control.
    """
    if structures > config.JOBS_MAX_STRUCTURES:
        raise JobsError(
            f"That's {structures} structures; the per-submission cap is "
            f"{config.JOBS_MAX_STRUCTURES}. Split it into smaller batches."
        )
    if structures <= 0:
        raise JobsError("No structures to submit.")

    wanted = structures * len(JOB_KINDS)
    mine, everyone = len(active_rows(requester_uid)), len(active_rows())
    over_user = mine + wanted > config.JOBS_MAX_ACTIVE_PER_USER
    over_all = everyone + wanted > config.JOBS_MAX_ACTIVE_TOTAL

    if over_user or over_all:
        # A row with no state counts as active, because treating "unknown" as
        # finished would let the caps be walked past by simply never reconciling.
        # But reconciliation is a manual CLI step, so without this the caps jam
        # permanently: a user whose jobs all finished weeks ago is still refused,
        # and told to "wait for some to finish" — advice that can never work.
        # Refresh once, from Slurm's own accounting, before refusing.
        try:
            reconcile(days=90)
        except JobsError:
            log.warning("jobs: could not reconcile before a cap check", exc_info=True)
        else:
            mine, everyone = len(active_rows(requester_uid)), len(active_rows())
            over_user = mine + wanted > config.JOBS_MAX_ACTIVE_PER_USER
            over_all = everyone + wanted > config.JOBS_MAX_ACTIVE_TOTAL

    if over_user:
        raise JobsError(
            f"You already have {mine} active Aspen jobs; this batch would add about "
            f"{wanted} and the per-user cap is {config.JOBS_MAX_ACTIVE_PER_USER}. "
            "Wait for some to finish, or cancel some."
        )
    if over_all:
        raise JobsError(
            f"Aspen has {everyone} active jobs across everyone, and the global cap is "
            f"{config.JOBS_MAX_ACTIVE_TOTAL}. Try again once the queue drains."
        )

    today = submits_today(requester_uid)
    if today >= config.JOBS_MAX_SUBMITS_PER_DAY:
        raise JobsError(
            f"You've made {today} submissions in the last 24 h, which is the daily "
            f"limit ({config.JOBS_MAX_SUBMITS_PER_DAY}). This resets on a rolling window."
        )


# --------------------------------------------------------------------------- #
# Dry-run → confirm tokens
# --------------------------------------------------------------------------- #
# In-process, single-use, TTL'd, and keyed by (thread, user). The confirmation has
# to be a Python control rather than a system-prompt instruction: a model can be
# talked out of asking, but it cannot mint a token it was never given. Same
# reasoning as pending.py, and as write_workflow taking its target from the event.
_PENDING: dict = {}


def issue_token(kind: str, requester_uid: str, thread_ts: str, payload: dict) -> str:
    token = uuid.uuid4().hex[:12]
    _PENDING[token] = {
        "kind": kind, "uid": requester_uid, "thread": thread_ts,
        "payload": payload, "at": time.monotonic(),
    }
    _expire_tokens()
    return token


def redeem_token(token: str, kind: str, requester_uid: str, thread_ts: str) -> dict:
    """Consume a token, or raise. Single-use even on a failed downstream action."""
    _expire_tokens()
    entry = _PENDING.pop((token or "").strip(), None)
    if entry is None:
        raise JobsError(
            "That confirmation has expired or was already used — run the dry run again."
        )
    if entry["kind"] != kind:
        raise JobsError("That confirmation was for a different action.")
    # The token is bound to who and where, so one cannot be replayed by another
    # user or carried into a different thread.
    if entry["uid"] != requester_uid or entry["thread"] != thread_ts:
        log.warning("jobs: token replay attempt by %s in %s", requester_uid, thread_ts)
        raise JobsError("That confirmation belongs to a different conversation.")
    return entry["payload"]


def _expire_tokens() -> None:
    cutoff = time.monotonic() - config.JOBS_CONFIRM_TTL
    for token in [t for t, e in _PENDING.items() if e["at"] < cutoff]:
        _PENDING.pop(token, None)


# --------------------------------------------------------------------------- #
# Submission
# --------------------------------------------------------------------------- #
def _run_pipeline(argv: list, cwd: Path) -> subprocess.CompletedProcess:
    """Run the pipeline entry point with a scrubbed environment.

    ``check=False``: a non-zero exit is a normal outcome to report back (bad
    inputs, a missing template), not an exception. The timeout matters because the
    orchestrator only *submits* — it does not wait for jobs — so a hang means
    something is wrong rather than something is slow.
    """
    return subprocess.run(
        argv, capture_output=True, text=True, check=False,
        cwd=str(cwd), timeout=config.JOBS_SUBMIT_TIMEOUT, env=submit_env(),
    )


def _pipeline_error_text(proc, limit: int = 1600) -> str:
    """The useful part of a failed pipeline run, for a Slack reply.

    Three things learned from running this against the real pipeline, none of
    which were guessable from the code:

    * The **reason** ("ERROR: Missing charge and/or multiplicity in XYZ header…")
      goes to *stderr*, while the *summary* ("ERROR: 12 of 12 XYZ file(s) failed",
      then a list of paths) goes to *stdout*. Reading either stream alone gives the
      user half the story — the paths without the cause, or the cause without
      knowing how much failed.
    * stderr also carries a Python traceback, so simply preferring stderr hands the
      user a stack dump and buries the sentence telling them what to fix.
    * A batch of 12 bad structures produces a page per structure, which is not a
      Slack message.

    So: pull the ERROR lines from *both* streams, de-duplicate, drop the traceback,
    and cap the result.
    """
    out, err = (proc.stdout or "").strip(), (proc.stderr or "").strip()

    lines, seen = [], set()
    for stream in (err, out):                    # cause first, then the summary
        for raw in stream.splitlines():
            line = raw.rstrip()
            stripped = line.strip()
            if not stripped or stripped.startswith(("Traceback", "File \"", "    ")):
                continue
            if stripped.startswith(("ERROR", "-", "WARNING")) or "ERROR:" in stripped:
                if stripped not in seen:
                    seen.add(stripped)
                    lines.append(line)

    text = "\n".join(lines)
    if not text:
        # Nothing recognisable — fall back to the tail of whichever stream spoke.
        tail = (out or err).splitlines()[-15:]
        text = "\n".join(tail) or f"exit {proc.returncode}"
    if len(text) > limit:
        kept = text[:limit].rsplit("\n", 1)[0]
        text = kept + f"\n… (truncated — {len(lines)} error lines in total)"
    return text


def dry_run(*, requester_uid: str, thread_ts: str, rel: str, owner: str,
            template_mode: str) -> dict:
    """Stage, validate, and run the pipeline with ``--no-submit``.

    Nothing is submitted. Returns a payload describing what *would* be, plus a
    single-use token the caller must redeem to commit — spec §19.7's dry-run-first
    requirement, with the confirmation enforced in Python rather than asked for in
    the prompt.
    """
    from . import staging

    if not config.JOBS_SUBMIT_ENABLED:
        raise JobsError(
            "Job submission is switched off on this deployment "
            "(ASPEN_JOBS_SUBMIT_ENABLED=false)."
        )
    require_registered(requester_uid)
    mode = (template_mode or "ca-fixed").strip().lower()
    staging.mode_flag(mode)                       # validate before doing any work

    structures, scope = staging.collect_structures(rel, owner, requester_uid)
    check_caps(requester_uid, len(structures))

    # Opportunistic, and here rather than on a cron because this is the moment
    # more staging is about to be created. Failures are logged, never fatal: a
    # housekeeping problem must not block a submission.
    try:
        prune_staging()
    except Exception:
        log.warning("jobs: staging prune failed", exc_info=True)

    staging_dir = staging.stage(
        requester_uid=requester_uid, thread_ts=thread_ts, structures=structures,
        scope=scope, template_mode=mode, source_rel=rel,
    )
    argv = build_submit_argv(staging_dir=staging_dir, out_dir=staging_dir,
                             template_mode=mode, dry_run=True)
    try:
        proc = _run_pipeline(argv, staging_dir)
    except subprocess.TimeoutExpired:
        _discard_staging(staging_dir)
        raise JobsError("The pipeline dry run timed out.") from None
    except OSError as exc:
        _discard_staging(staging_dir)
        raise JobsError(
            f"Could not run {config.JOBS_PIPELINE_BIN!r}: {exc}. Is the pipeline "
            "installed and on PATH?"
        ) from exc

    if proc.returncode != 0:
        # A rejected dry run leaves nothing behind. Otherwise a user iterating on
        # bad inputs silently accumulates a staging directory per attempt, each
        # holding a full copy of their structures.
        _discard_staging(staging_dir)
        raise JobsError(
            "The pipeline rejected these inputs during the dry run:\n"
            + _pipeline_error_text(proc)
        )

    payload = {
        "rel": rel, "owner": owner, "template_mode": mode,
        "staging_dir": str(staging_dir),
        "structures": [s.name for s in structures],
        "scope": scope.get("name", ""),
        "project": _project_of(rel),
    }
    token = issue_token("submit", requester_uid, thread_ts, payload)
    return {**payload, "token": token, "stdout": proc.stdout[-4000:]}


def commit(*, requester_uid: str, thread_ts: str, token: str,
           channel: str = "", notify: bool = False) -> dict:
    """Redeem a dry-run token and submit for real.

    The ledger row is written **before** the pipeline runs and a failure to write
    aborts: a job with no ledger row is one nobody can cancel through Aspen and
    nobody can attribute, which is strictly worse than not submitting.
    """
    from . import staging

    if not config.JOBS_SUBMIT_ENABLED:
        raise JobsError("Job submission is switched off on this deployment.")

    require_registered(requester_uid)
    payload = redeem_token(token, "submit", requester_uid, thread_ts)
    staging_dir = Path(payload["staging_dir"])
    if not _within(staging_dir, user_staging_root(requester_uid)):
        # Cannot happen via dry_run, which derives the path; this catches a future
        # caller that starts trusting a client-supplied payload.
        raise JobsError("Internal error: that staging directory is not yours.")
    if not staging_dir.is_dir():
        raise JobsError("The staged inputs have gone — run the dry run again.")

    structures = payload["structures"]
    check_caps(requester_uid, len(structures))     # re-check: time passed

    # The dry run wrote its own batch-jobs.log into this same directory, with
    # SKIPPED rows and no job IDs. Leaving it means the log describes two runs and
    # the parse has to guess which rows are live — which is exactly how the first
    # real submission ended up reporting "0 jobs recorded" for three jobs that had
    # actually landed. Move it aside so the log describes this run and no other.
    stale = staging_dir / "batch-jobs.log"
    if stale.is_file():
        try:
            stale.rename(staging_dir / "batch-jobs.dry-run.log")
        except OSError:
            log.warning("jobs: could not set aside the dry run's log", exc_info=True)

    user = registry.by_id(requester_uid) or {}
    argv = build_submit_argv(staging_dir=staging_dir, out_dir=staging_dir,
                             template_mode=payload["template_mode"], dry_run=False)

    batch_id = record_batch(
        slack_user_id=requester_uid, alias=user.get("alias", "unknown"),
        thread_ts=thread_ts, project=payload.get("project", ""),
        owner_scope=payload.get("scope", ""), template_mode=payload["template_mode"],
        staging_dir=staging_dir, structures=len(structures), argv=argv,
        channel=channel, notify=notify,
    )

    try:
        proc = _run_pipeline(argv, staging_dir)
    except subprocess.TimeoutExpired:
        # The pipeline may well have submitted before it hung. Recover whatever it
        # wrote, so the batch is cancellable rather than orphaned.
        recovered = backfill(batch_id).get("jobs_recovered", 0)
        raise JobsError(
            f"Submission timed out after {config.JOBS_SUBMIT_TIMEOUT}s "
            f"(batch {batch_id}). "
            + (f"{recovered} job(s) had already been submitted and are now tracked — "
               "you can list or cancel them."
               if recovered else
               "No job IDs were found, so nothing appears to have been submitted.")
        ) from None
    except OSError as exc:
        raise JobsError(f"Could not run the pipeline: {exc}") from exc

    entries = staging.parse_submitted_jobs(proc.stdout, staging_dir)
    recorded = record_jobs(batch_id, entries)

    if proc.returncode != 0:
        # Partial submission is possible: the pipeline submits per structure, so
        # some jobs may exist. Whatever was parsed is already in the ledger, which
        # is what makes them cancellable.
        raise JobsError(
            f"The pipeline reported an error (batch {batch_id}, {recorded} job(s) "
            "recorded so far):\n"
            + _pipeline_error_text(proc)
        )

    log.info("jobs: batch %s submitted %d job(s) for %s", batch_id, recorded, requester_uid)
    return {
        "batch_id": batch_id, "jobs": entries, "recorded": recorded,
        "structures": structures, "staging_dir": str(staging_dir),
        "stdout": proc.stdout[-4000:],
    }


def _discard_staging(staging_dir: Path) -> None:
    """Remove a staging directory, but only ever one inside the staging root."""
    try:
        if _within(staging_dir, config.JOBS_STAGING_ROOT):
            shutil.rmtree(staging_dir, ignore_errors=True)
    except Exception:
        log.warning("jobs: could not clean up %s", staging_dir, exc_info=True)


def backfill_jobs(batch_id: str = "") -> dict:
    """Re-read staging for batches that recorded no jobs, and record what is there.

    Exists because the first live submission produced three real Slurm jobs and zero
    ledger rows: the log parser was wrong about the format. A batch in that state is
    uncancellable through Aspen even though the jobs are running, which is the one
    outcome the ledger is supposed to make impossible — so recovery has to be
    possible without hand-editing a database.

    Safe to re-run: rows are inserted with ``INSERT OR IGNORE``, and only batches
    with **no** jobs are touched, so it can never contradict what is already recorded.
    """
    from . import staging

    with connect() as conn:
        if batch_id:
            rows = conn.execute("SELECT * FROM batches WHERE batch_id = ?",
                                (batch_id,)).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM batches b WHERE NOT EXISTS "
                "(SELECT 1 FROM jobs j WHERE j.batch_id = b.batch_id)").fetchall()

    repaired, still_empty = [], []
    for row in (dict(r) for r in rows):
        staging_dir = Path(row["staging_dir"])
        if not staging_dir.is_dir():
            still_empty.append((row["batch_id"], "staging directory is gone"))
            continue
        entries = staging.parse_submitted_jobs("", staging_dir)
        if not entries:
            still_empty.append((row["batch_id"], "no job IDs in batch-jobs.log"))
            continue
        n = record_jobs(row["batch_id"], entries)
        repaired.append((row["batch_id"], n))
        log.info("jobs: backfilled %d job(s) for batch %s", n, row["batch_id"])
    return {"repaired": repaired, "still_empty": still_empty}


def prune_staging(max_age_hours: float = 48.0) -> dict:
    """Delete staging directories no batch ever claimed.

    A dry run stages a full copy of the user's structures *before* the pipeline
    validates them, because the pipeline is what does the validating. If the user
    then says "no thanks" — the correct and expected outcome of a preview — that
    copy is orphaned. Only *failed* dry runs clean up after themselves, so without
    this, ordinary polite use grows the staging tree without bound.

    Safe by construction: a directory is removed only when **no ledger row points
    at it** (so nothing submitted is ever touched, however old) and it is older
    than ``max_age_hours`` (so a preview waiting on a confirmation survives).
    """
    root = config.JOBS_STAGING_ROOT
    if not root.is_dir():
        return {"removed": 0, "kept": 0, "bytes": 0}

    with connect() as conn:
        claimed = {row["staging_dir"] for row in conn.execute(
            "SELECT staging_dir FROM batches")}

    cutoff = time.time() - max_age_hours * 3600
    removed, kept, freed = 0, 0, 0
    for user_dir in root.iterdir():
        if not user_dir.is_dir():
            continue
        for run_dir in user_dir.iterdir():
            if not run_dir.is_dir():
                continue
            if str(run_dir) in claimed:
                kept += 1
                continue
            try:
                if run_dir.stat().st_mtime > cutoff:
                    kept += 1
                    continue
                freed += sum(f.stat().st_size for f in run_dir.rglob("*") if f.is_file())
            except OSError:
                kept += 1
                continue
            _discard_staging(run_dir)
            removed += 1
    if removed:
        log.info("jobs: pruned %d abandoned staging dir(s), %.1f MB", removed, freed / 1e6)
    return {"removed": removed, "kept": kept, "bytes": freed}


def backfill(batch_id: str = "") -> dict:
    """Recover job IDs the submission step failed to capture.

    The pipeline writes ``batch-jobs.log`` as it submits, so the IDs exist on disk
    even when Aspen never read them — because the subprocess timed out, or the parse
    ran against a half-written file, or the process died between sbatch and the
    ledger write. Without this, that batch is permanently invisible: not listed, not
    cancellable, not attributable, while its jobs burn real compute.

    Self-healing rather than a repair tool: called automatically wherever the ledger
    is read, so the gap closes on its own instead of waiting for someone to notice.
    """
    from . import staging as staging_mod

    with connect() as conn:
        sql = ("SELECT b.batch_id, b.staging_dir FROM batches b "
               "LEFT JOIN jobs j ON j.batch_id = b.batch_id "
               "WHERE j.job_id IS NULL")
        params = []
        if batch_id:
            sql += " AND b.batch_id = ?"
            params.append(batch_id)
        empty = [dict(r) for r in conn.execute(sql, params)]

    found = 0
    for row in empty:
        staging_dir = Path(row["staging_dir"])
        if not staging_dir.is_dir():
            continue
        entries = staging_mod.parse_submitted_jobs("", staging_dir)
        if entries:
            found += record_jobs(row["batch_id"], entries)
            log.info("jobs: backfilled %d job id(s) for batch %s from %s",
                     len(entries), row["batch_id"], staging_dir / "batch-jobs.log")
    return {"batches_checked": len(empty), "jobs_recovered": found}


def _project_of(rel: str) -> str:
    """The project name a relative path sits under — its first component."""
    parts = [p for p in str(rel).replace("\\", "/").split("/") if p and p not in (".", "..")]
    if parts and parts[0].startswith("@"):
        parts = parts[1:]
    return parts[0] if parts else ""


# --------------------------------------------------------------------------- #
# Reconciliation
# --------------------------------------------------------------------------- #
def reconcile(days: int = 30) -> dict:
    """Join the ledger against Slurm's own accounting.

    Attribution is two-phase and this is the phase that answers "who used the
    compute": at submit time you know who and what, but elapsed time, CPU-hours and
    exit state exist only after a job ends. The two copies also fail differently —
    the ledger can be deleted and Slurm's copy survives; slurmdbd purges on a
    site schedule and the ledger survives that.
    """
    since = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")
    argv = build_sacct_argv(user=whoami(), since=since)
    try:
        proc = _run_slurm(argv)
    except (OSError, subprocess.SubprocessError) as exc:
        raise JobsError(f"Could not run sacct: {exc}") from exc
    if proc.returncode != 0:
        raise JobsError("sacct failed: " + (proc.stderr.strip() or f"exit {proc.returncode}"))

    known = {str(r["job_id"]) for r in _all_job_ids()}
    rows = []
    for line in proc.stdout.splitlines():
        f = line.split("|")
        if len(f) < 11 or f[0] not in known:
            continue
        rows.append({
            "job_id": f[0], "state": f[3], "elapsed": f[7],
            "total_cpu": f[8], "alloc_tres": f[9], "exit_code": f[10],
        })
    return {"updated": apply_reconciliation(rows), "scanned": len(rows), "since": since}


def _all_job_ids() -> list:
    with connect() as conn:
        return [dict(r) for r in conn.execute("SELECT job_id FROM jobs")]


# --------------------------------------------------------------------------- #
# Direct submission — Aspen builds the sbatch argv itself
#
# This is the path for a `direct` runner (spec §20): one job, one input file, from
# a registered job script. It differs from the pipeline path in a way that matters
# for the cancel boundary — because Aspen calls `sbatch` here rather than delegating
# to an orchestrator, it CAN set `--comment`, which the pipeline path cannot. So
# these jobs carry the per-user tag the original design (§18.2) wanted, on top of
# the WorkDir fence.
# --------------------------------------------------------------------------- #
COMMENT_PREFIX = "aspen/v1"
_COMMENT_RE = re.compile(r"^aspen/v1/[A-Z0-9]+/[0-9.]+$")


def build_comment(requester_uid: str, thread_ts: str) -> str:
    """The durable machine tag: ``aspen/v1/<slack-id>/<thread-ts>``.

    Composed only from the Slack event's own fields — never from conversation text
    — which is the property that makes it un-forgeable (C9). The ID rather than the
    alias, because aliases are renameable and a tag baked into a months-old job
    record must still resolve.
    """
    if not _SLACK_ID_RE.match(requester_uid or ""):
        raise JobsError("Internal error: cannot tag a job without a Slack user ID.")
    thread = re.sub(r"[^0-9.]", "", str(thread_ts)) or "0"
    comment = f"{COMMENT_PREFIX}/{requester_uid}/{thread}"
    if not _COMMENT_RE.match(comment):
        raise JobsError("Internal error: refusing to build a malformed job tag.")
    return comment


def build_job_name(alias: str, label: str) -> str:
    """``aspen-<alias>-<label>`` — what the group sees in ``squeue``.

    Human-facing only. Never an authorization key: aliases are renameable, names
    are not unique, and a human can type one by hand.
    """
    safe = re.sub(r"[^A-Za-z0-9]+", "-", f"{alias}-{label}").strip("-")[:48]
    return f"aspen-{safe}" if safe else "aspen-job"


def build_sbatch_argv(*, script_name: str, job_name: str, comment: str,
                      chdir: Path) -> list:
    """``sbatch --parsable --job-name=… --comment=… --chdir=… script``.

    A list, built here, with no user-supplied flags anywhere — which is the whole
    reason ``sbatch`` is a structured tool instead of a Bash allowlist entry. Every
    argument is either a literal, a value this module derived, or a name validated
    to ``[A-Za-z0-9._-]``. ``--parsable`` makes stdout just the job ID, so parsing
    cannot be the weak link it was on the pipeline path.

    ``--wrap`` is the flag this design exists to avoid, so it is asserted absent
    rather than merely left out.
    """
    if not re.fullmatch(r"[A-Za-z0-9._-]+", script_name or ""):
        raise JobsError(f"{script_name!r} is not a usable script filename")
    if not re.fullmatch(r"[A-Za-z0-9._-]+", job_name or ""):
        raise JobsError(f"{job_name!r} is not a usable job name")
    if not _COMMENT_RE.match(comment or ""):
        raise JobsError("refusing to submit without a well-formed Aspen tag")

    argv = [
        "sbatch", "--parsable",
        f"--job-name={job_name}",
        f"--comment={comment}",
        f"--chdir={chdir}",
        script_name,
    ]
    if any("--wrap" in a for a in argv):        # pragma: no cover — belt and braces
        raise JobsError("refusing to build an sbatch with --wrap")
    return argv


def _parse_job_id(stdout: str) -> str:
    """The job ID from ``sbatch --parsable`` output (``12345`` or ``12345;cluster``)."""
    first = (stdout or "").strip().splitlines()[0] if (stdout or "").strip() else ""
    candidate = first.split(";")[0].strip()
    if not _JOB_ID_RE.match(candidate):
        raise JobsError(f"could not read a job ID from sbatch output: {stdout[:200]!r}")
    return candidate


def prepare_direct(*, requester_uid: str, thread_ts: str, template: str,
                   owner: str = "", runner: str = "", charge: Optional[int] = None,
                   multiplicity: Optional[int] = None, geometry_path: str = "",
                   geometry_owner: str = "", ntasks: Optional[int] = None,
                   mem_gb: Optional[int] = None, time_limit: str = "",
                   label: str = "") -> dict:
    """Build everything a direct submission needs, stage it, and return a preview.

    Nothing is submitted. Returns the rendered input, the diff against the template
    it came from, and a single-use token — so the user sees exactly what changed
    before agreeing to spend compute on it.
    """
    from . import inputs, runners, staging as staging_mod, templates

    require_registered(requester_uid)
    if not config.JOBS_SUBMIT_ENABLED:
        raise JobsError("Job submission is switched off on this deployment.")

    profile = runners.for_user(requester_uid, runner)
    if profile is None:
        saved = [e["name"] for e in runners.index(requester_uid) if e["mine"]]
        raise JobsError(
            "I don't know which job script to use. "
            + (f"You have several saved ({', '.join(saved)}) — name one."
               if saved else
               "You have none saved yet. Show me the job script you normally use and "
               "I'll check it over and save it as a runner.")
        )
    code = profile.get("code", "orca")

    # The starting point: one of the requester's templates, or a colleague's by
    # name. A read, fenced like every other read.
    original, meta = templates.resolve(template, requester_uid, owner)
    inputs.check(original, code)            # stored file, current rules

    text = original
    coordinates = None
    if geometry_path:
        path, scope, error = roots_resolve(geometry_path, geometry_owner, requester_uid)
        if error:
            raise JobsError(error)
        if path.suffix.lower() != ".xyz":
            raise JobsError(f"'{geometry_path}' is not an .xyz structure file.")
        coordinates = inputs.coordinates_from_xyz(
            path.read_text(encoding="utf-8", errors="replace")
        )

    if any(v is not None for v in (charge, multiplicity)) or coordinates:
        text = inputs.replace_geometry(
            text, charge=charge, multiplicity=multiplicity, coordinates=coordinates
        )
    inputs.check(text, code)                # and again after editing

    # Stage: the input and the rendered script, in the requester's own area.
    staging_dir = staging_mod.new_run_dir(requester_uid, thread_ts)
    stem = re.sub(r"[^A-Za-z0-9._-]+", "-", (label or meta.get("name") or "job")).strip("-")[:40]
    stem = stem or "job"
    input_name, output_name = f"{stem}.inp", f"{stem}.out"
    script_name = "aspen-job.sh"

    (staging_dir / input_name).write_text(text, encoding="utf-8")
    script = runners.render(
        profile, job_name=build_job_name(
            (registry.by_id(requester_uid) or {}).get("alias", "user"), stem),
        input_name=input_name, output_name=output_name,
        ntasks=ntasks, mem_gb=mem_gb, time_limit=time_limit,
    )
    (staging_dir / script_name).write_text(script, encoding="utf-8")
    (staging_dir / script_name).chmod(0o700)

    payload = {
        "runner": profile["name"], "code": code, "template": meta.get("name", template),
        "template_owner": meta.get("owner_alias", ""),
        "staging_dir": str(staging_dir), "input_name": input_name,
        "output_name": output_name, "script_name": script_name,
        "label": stem, "geometry_path": geometry_path,
    }
    token = issue_token("submit_direct", requester_uid, thread_ts, payload)
    return {**payload, "token": token, "input_text": text,
            "diff": _unified_diff(original, text, meta.get("name", template), input_name)}


def roots_resolve(rel: str, owner: str, viewer_uid: str):
    """Indirection so the geometry lookup goes through the one fenced seam."""
    from . import roots
    return roots.resolve(rel, owner, viewer_uid)


def _unified_diff(before: str, after: str, before_name: str, after_name: str,
                  limit: int = 120) -> str:
    """What changed, for the user to read before agreeing.

    The human review step is doing real work here: the people using this are domain
    experts who will spot a wrong functional or a missing constraint immediately,
    which no validator can do.
    """
    import difflib
    lines = list(difflib.unified_diff(
        before.splitlines(), after.splitlines(),
        fromfile=before_name, tofile=after_name, lineterm="", n=2,
    ))
    if not lines:
        return "(no change from the template)"
    if len(lines) > limit:
        lines = lines[:limit] + [f"… ({len(lines) - limit} more diff lines)"]
    return "\n".join(lines)


def commit_direct(*, requester_uid: str, thread_ts: str, token: str,
                  channel: str = "", notify: bool = False) -> dict:
    """Redeem a prepared submission and run ``sbatch``. Ledger first, always."""
    require_registered(requester_uid)
    if not config.JOBS_SUBMIT_ENABLED:
        raise JobsError("Job submission is switched off on this deployment.")

    payload = redeem_token(token, "submit_direct", requester_uid, thread_ts)
    staging_dir = Path(payload["staging_dir"])
    if not _within(staging_dir, user_staging_root(requester_uid)):
        raise JobsError("Internal error: that staging directory is not yours.")
    if not (staging_dir / payload["script_name"]).is_file():
        raise JobsError("The staged job has gone — prepare it again.")

    check_caps(requester_uid, 1)
    user = registry.by_id(requester_uid) or {}
    comment = build_comment(requester_uid, thread_ts)
    job_name = build_job_name(user.get("alias", "user"), payload["label"])
    argv = build_sbatch_argv(script_name=payload["script_name"], job_name=job_name,
                            comment=comment, chdir=staging_dir)

    batch_id = record_batch(
        slack_user_id=requester_uid, alias=user.get("alias", "unknown"),
        thread_ts=thread_ts, project=payload.get("template", ""),
        owner_scope=payload.get("template_owner", ""),
        template_mode=f"{payload['runner']}:{payload['template']}",
        staging_dir=staging_dir, structures=1, argv=argv,
        channel=channel, notify=notify,
    )

    try:
        proc = _run_pipeline(argv, staging_dir)
    except subprocess.TimeoutExpired:
        raise JobsError(f"sbatch timed out (batch {batch_id}).") from None
    except OSError as exc:
        raise JobsError(f"Could not run sbatch: {exc}") from exc

    if proc.returncode != 0:
        raise JobsError(
            f"sbatch refused the job (batch {batch_id}):\n" + _pipeline_error_text(proc)
        )

    job_id = _parse_job_id(proc.stdout)
    record_jobs(batch_id, [{
        "job_id": job_id, "kind": payload["code"], "job_name": job_name,
        "work_dir": str(staging_dir), "comment": comment,
    }])
    log.info("jobs: batch %s submitted direct job %s for %s", batch_id, job_id, requester_uid)
    return {"batch_id": batch_id, "job_id": job_id, "job_name": job_name,
            "staging_dir": str(staging_dir), "input_name": payload["input_name"],
            "runner": payload["runner"]}
