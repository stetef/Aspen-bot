"""
Telling someone their jobs finished.

Aspen's silence used to carry no information: a batch could complete, fail, or sit
in the queue for a day, and the only way to find out was to ask. That is a poor
deal when the whole point of submitting through Aspen is not having to babysit the
queue — so a user can opt in, once, and be told.

Three decisions worth stating, because each could reasonably have gone the other way:

**A failure notifies immediately; success waits for the whole batch.** Waiting for
every job to reach a terminal state before saying anything would mean a run that
died in its first minute stays silent for the hours its dependents spend queued —
and a dependency chain does not fail fast, it fails *late*. So the moment any job in
a batch reaches a failed state, that is worth interrupting for. A clean run is only
interesting once it is actually done.

**One notification per batch.** Not one per job: a nine-job batch would be nine
pings for a single piece of news. The message names what failed, which is the part
you would otherwise go looking for.

**Two cadences, not one.** A single poll interval had to be a compromise between
telling someone promptly and not polling Slurm's accounting database forever, and
it settled on five minutes — which is a long time to sit on news the cluster
already has. It does not have to be a compromise, because the two states cost
different amounts: an idle pass is one ledger ``COUNT``, and an active pass is a
``squeue`` against controller memory that reaches ``sacct`` only once something has
actually left the queue (``jobs.refresh_states``). So the loop watches often while
there is something to watch, and rarely when there is not.

**In the thread, falling back to a DM.** The thread is where the context is — the
diff that was approved, the structures that were chosen. But threads can outlive
their channel, and Slack refuses posts to conversations the bot has been removed
from, so a failed post retries as a direct message rather than being dropped.

The preference is stored per user (``notify_jobs`` on the registry record) so nobody
is asked twice. Like ``declined`` (§5.0), this is one the agent may write: it steers
whether Aspen speaks to *that person* about *their own* jobs, and cannot reach
anyone else or grant anything.
"""

import logging
import threading
import time
from datetime import datetime, timezone

from . import config, jobs, registry, results

log = logging.getLogger("aspen")

# Values for the registry's ``notify_jobs`` field.
ALWAYS, NEVER, UNSET = "always", "never", ""
CHOICES = (ALWAYS, NEVER)

# States that mean "this went wrong", as opposed to merely finished.
FAILED_STATES = frozenset({
    "FAILED", "TIMEOUT", "NODE_FAIL", "BOOT_FAIL", "DEADLINE",
    "OUT_OF_MEMORY", "PREEMPTED", "REVOKED", "SPECIAL_EXIT",
})


def preference(uid: str) -> str:
    """``always``, ``never``, or empty when they have not been asked yet."""
    user = registry.by_id(uid) or {}
    value = (user.get("notify_jobs") or "").strip().lower()
    return value if value in CHOICES else UNSET


def set_preference(uid: str, value: str) -> str:
    """Record whether this person wants to be told when their jobs finish.

    Agent-writable, for the reason ``setup.decline`` is: it decides whether Aspen
    speaks to the person who set it about their own jobs. It grants nothing, reaches
    nobody else, and the worst a confused agent achieves is an unwanted message or a
    missing one.
    """
    choice = (value or "").strip().lower()
    if choice not in CHOICES:
        return f"Error: choose one of {', '.join(CHOICES)}."
    user = registry.by_id(uid)
    if user is None:
        return "Error: you are not in Aspen's user registry, so there's nothing to record."

    entries = []
    for entry in registry.users(include_removed=True):
        if entry["slack_user_id"] == uid:
            entry = dict(entry, notify_jobs=choice)
        entries.append(entry)
    registry.save(entries)
    log.info("notify: %s set job notifications to %s", user["alias"], choice)
    return ("I'll tell you when your jobs finish, and straight away if one fails."
            if choice == ALWAYS else
            "I won't ping you about jobs — ask me any time and I'll check.")


# --------------------------------------------------------------------------- #
# Deciding what is due
# --------------------------------------------------------------------------- #
def due() -> list:
    """Batches that should be notified about now, with the message to send.

    Pure with respect to Slack — it reads the ledger and returns intentions, so the
    decision can be tested without a workspace, a network, or a bot token.
    """
    jobs.refresh_states()

    out = []
    with jobs.connect() as conn:
        batches = [dict(r) for r in conn.execute(
            "SELECT * FROM batches WHERE notify = 1 AND notified_at IS NULL")]

    for batch in batches:
        rows = jobs.jobs_for_batch(batch["batch_id"])
        if not rows:
            continue                      # nothing recorded yet; backfill may fix it
        states = {(r.get("state") or "").upper() for r in rows}
        failed = [r for r in rows if (r.get("state") or "").upper() in FAILED_STATES]
        settled = all((r.get("state") or "").upper() in jobs.TERMINAL_STATES
                      or r.get("cancelled_at") for r in rows)

        if failed:
            reason = "failed"
        elif settled:
            reason = "finished"
        else:
            continue                      # still running: nothing to say yet

        out.append({
            "batch_id": batch["batch_id"],
            "slack_user_id": batch["slack_user_id"],
            "channel": batch.get("channel") or "",
            "thread_ts": batch.get("thread_ts") or "",
            "reason": reason,
            "text": _message(batch, rows, failed, reason),
        })
    return out


def _message(batch: dict, rows: list, failed: list, reason: str) -> str:
    """What the user actually reads. Specific enough to act on without asking."""
    label = batch.get("project") or batch.get("template_mode") or "your batch"
    head = (f":warning: Your `{label}` batch hit a problem "
            f"(batch `{batch['batch_id']}`)."
            if reason == "failed" else
            f":white_check_mark: Your `{label}` batch has finished "
            f"(batch `{batch['batch_id']}`).")

    lines = [head]
    for row in rows[:8]:
        state = (row.get("state") or "unknown").upper()
        mark = "✗" if state in FAILED_STATES else "✓"
        detail = f" — {row['elapsed']}" if row.get("elapsed") else ""
        lines.append(f"  {mark} {row['job_id']} {row.get('kind') or 'job'} "
                     f"{state}{detail}")
    if len(rows) > 8:
        lines.append(f"  … and {len(rows) - 8} more")

    if failed:
        lines.append("")
        lines.append("Ask me to look at the output and I'll pull the log and say what "
                     "went wrong.")

    # Both halves of "where did it go", because they answer different questions and
    # only one of them used to be here. The path alone left the user to work out the
    # copy themselves, and left Aspen unable to open the file it had just announced.
    lines.append(f"\nResults are in `{batch.get('staging_dir', '')}` — ask me and I'll "
                 "read them from there.")
    command = results.copy_command(batch, batch.get("slack_user_id", ""))
    if command:
        lines.append(f"To keep a copy in your own tree:\n```{command}```")
    return "\n".join(lines)


def mark_sent(batch_id: str) -> None:
    with jobs.connect() as conn:
        conn.execute("UPDATE batches SET notified_at = ? WHERE batch_id = ?",
                     (datetime.now(timezone.utc).isoformat(timespec="seconds"), batch_id))


# --------------------------------------------------------------------------- #
# Sending
# --------------------------------------------------------------------------- #
def send(client, item: dict) -> bool:
    """Post one notification. In-thread first, DM as the fallback.

    Marked as sent whether or not delivery succeeded, because the alternative is a
    batch that retries forever against a channel the bot was removed from — a
    notification is worth one good attempt, not an unbounded one.
    """
    text, delivered = item["text"], False
    if item["channel"] and item["thread_ts"]:
        try:
            client.chat_postMessage(channel=item["channel"],
                                    thread_ts=item["thread_ts"], text=text)
            delivered = True
        except Exception as exc:
            log.warning("notify: could not post to %s/%s (%s); trying a DM",
                        item["channel"], item["thread_ts"], exc)
    if not delivered:
        try:
            client.chat_postMessage(channel=item["slack_user_id"], text=text)
            delivered = True
        except Exception:
            log.warning("notify: could not DM %s either", item["slack_user_id"],
                        exc_info=True)

    mark_sent(item["batch_id"])
    return delivered


def run_once(client) -> int:
    """One pass. Returns how many notifications went out."""
    sent = 0
    for item in due():
        if send(client, item):
            sent += 1
    return sent


# Floor on either interval. Lower than the 60s this used to be pinned at, because
# what a pass costs changed: with nothing outstanding it is one ledger COUNT and no
# Slurm call, and with jobs in flight it is a squeue against controller memory that
# only escalates to sacct when something has actually left the queue.
MIN_INTERVAL = 30


def interval_for(outstanding: int) -> int:
    """How long to sleep after a pass, given what is still in flight.

    One interval had to serve two situations that want opposite things: a user
    waiting on a batch wants to hear quickly, and a bot with an empty ledger should
    not be waking up every minute for years. Splitting them costs nothing, since
    the frequent case is also the cheap one.
    """
    active = max(MIN_INTERVAL, config.JOBS_NOTIFY_ACTIVE_POLL_SECONDS)
    idle = max(active, config.JOBS_NOTIFY_POLL_SECONDS)
    return active if outstanding else idle


def watcher(client, stop: threading.Event = None) -> None:
    """The loop. Started as a daemon thread at boot; never raises out.

    A failure here must not take the bot down or stop future passes — the worst
    outcome of a bad poll is a late notification, and the worst outcome of an
    unhandled exception would be no notifications at all, silently.
    """
    log.info("Job notifications: watching every %ds while jobs are outstanding, "
             "%ds when the ledger is quiet",
             interval_for(1), interval_for(0))
    while not (stop and stop.is_set()):
        interval = interval_for(0)
        try:
            count = run_once(client)
            if count:
                log.info("notify: sent %d job notification(s)", count)
            interval = interval_for(jobs.outstanding_count())
        except Exception:
            log.warning("notify: poll failed; will try again", exc_info=True)
        if stop:
            stop.wait(interval)
        else:                                   # pragma: no cover — production path
            time.sleep(interval)


def start(client) -> threading.Thread:
    thread = threading.Thread(target=watcher, args=(client,),
                              name="aspen-notify", daemon=True)
    thread.start()
    return thread
