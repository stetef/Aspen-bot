"""
Pending asks for the admin — who wants in, and who wants a calculations root.

Aspen deliberately cannot grant either one: admission and root paths are
CLI-only, so that no message, however phrased, can widen access or point someone
at an arbitrary directory (see registry.py and roots.py). But "the agent cannot
do it" should not mean "the person has to work out who to ask and what to type".

So a request is *recorded* and the admin is *told*, with the exact command to run
if they agree. The decision stays a human one; only the paperwork is automated.

The store doubles as the admin's queue::

    $ASPEN_STATE_DIR/requests.json

It lives beside the registry — outside WORKSPACE_ROOT and every sandbox-writable
path — for the same reason the registry does: state that steers what the operator
does should not be writable by sandboxed code.

Notifications are **de-duplicated and rate-limited**. Someone who messages an
unauthorized bot five times in a minute is one request, not five DMs, and a
restart loop must not turn into a pager.
"""

import json
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Optional

from . import config, registry

log = logging.getLogger("aspen")

KINDS = ("access", "calc_root")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _stamp(when: Optional[datetime] = None) -> str:
    return (when or _now()).strftime("%Y-%m-%dT%H:%MZ")


def _parse(stamp: str) -> Optional[datetime]:
    try:
        return datetime.strptime(stamp, "%Y-%m-%dT%H:%MZ").replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None


# --------------------------------------------------------------------------- #
# Store
# --------------------------------------------------------------------------- #
def load() -> list[dict]:
    """Every open request. A malformed file is never fatal — it is a queue, not
    an authorization record, so losing it costs a reminder and nothing else."""
    try:
        raw = json.loads(config.REQUESTS_FILE.read_text())
    except FileNotFoundError:
        return []
    except (OSError, ValueError):
        log.warning("requests: %s is unreadable — treating the queue as empty",
                    config.REQUESTS_FILE)
        return []
    out = []
    for entry in raw.get("requests") or []:
        if isinstance(entry, dict) and entry.get("kind") in KINDS and entry.get("slack_user_id"):
            out.append(entry)
    return out


def save(entries: list[dict]) -> None:
    registry.ensure_private_dir(config.STATE_DIR)
    payload = {"version": 1, "requests": entries}
    tmp = config.REQUESTS_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    os.chmod(tmp, 0o600)
    os.replace(tmp, config.REQUESTS_FILE)


def find(kind: str, uid: str) -> Optional[dict]:
    for entry in load():
        if entry["kind"] == kind and entry["slack_user_id"] == uid:
            return entry
    return None


def record(kind: str, uid: str, display_name: str = "", detail: str = "") -> dict:
    """Add or refresh a request. Returns the stored entry.

    Repeat asks bump a counter rather than piling up: the admin wants to know
    that someone is waiting and how keenly, not to read the same line five times.
    """
    if kind not in KINDS:
        raise ValueError(f"unknown request kind: {kind!r}")
    entries = load()
    for entry in entries:
        if entry["kind"] == kind and entry["slack_user_id"] == uid:
            entry["last_seen"] = _stamp()
            entry["count"] = int(entry.get("count", 1)) + 1
            if display_name:
                entry["display_name"] = display_name
            if detail:
                entry["detail"] = detail
            save(entries)
            return entry

    entry = {
        "kind": kind,
        "slack_user_id": uid,
        "display_name": display_name or uid,
        "detail": detail,
        "first_seen": _stamp(),
        "last_seen": _stamp(),
        "count": 1,
        "notified": "",
    }
    entries.append(entry)
    save(entries)
    return entry


def resolve(kind: str, uid: str) -> bool:
    """Drop a request — it was granted, or declined, or is simply stale."""
    entries = load()
    keep = [e for e in entries if not (e["kind"] == kind and e["slack_user_id"] == uid)]
    if len(keep) == len(entries):
        return False
    save(keep)
    return True


def resolve_satisfied() -> list[dict]:
    """Drop requests reality has already answered.

    Cheaper than asking the admin to tidy up: if someone was added, or their root
    was set, the ask is no longer pending whether or not anyone said so.
    """
    entries, dropped = load(), []
    keep = []
    for entry in entries:
        user = registry.by_id(entry["slack_user_id"], include_removed=False)
        satisfied = (
            (entry["kind"] == "access" and user is not None)
            or (entry["kind"] == "calc_root" and user is not None and user.get("calc_root"))
        )
        (dropped if satisfied else keep).append(entry)
    if dropped:
        save(keep)
    return dropped


# --------------------------------------------------------------------------- #
# What the admin needs to do about it
# --------------------------------------------------------------------------- #
def command_for(entry: dict) -> str:
    """The exact line to run if the admin agrees. Copy-paste, no thinking."""
    uid = entry["slack_user_id"]
    if entry["kind"] == "access":
        alias = registry.slugify(entry.get("display_name", "")) or uid.lower()
        name = entry.get("display_name") or alias
        return f'./aspen-users add {uid} --alias {alias} --name "{name}"'
    user = registry.by_id(uid)
    who = user["alias"] if user else uid
    path = entry.get("detail") or "<path-to-their-calculations>"
    return f"./aspen-users set-root {who} {path}"


def describe(entry: dict) -> str:
    """One human line for the DM and the CLI listing."""
    who = entry.get("display_name") or entry["slack_user_id"]
    repeats = f" (asked {entry['count']}×)" if int(entry.get("count", 1)) > 1 else ""
    if entry["kind"] == "access":
        return f"{who} <{entry['slack_user_id']}> wants access{repeats}"
    detail = entry.get("detail")
    where = f" pointing at `{detail}`" if detail else " (they didn't say where yet)"
    return f"{who} wants their own calculations root{where}{repeats}"


def due_for_notice(entry: dict) -> bool:
    """True if the admin has not been told recently enough to stay quiet."""
    last = _parse(entry.get("notified", ""))
    if last is None:
        return True
    return _now() - last >= timedelta(hours=config.REQUEST_NOTIFY_COOLDOWN_HOURS)


def mark_notified(kind: str, uid: str) -> None:
    entries = load()
    for entry in entries:
        if entry["kind"] == kind and entry["slack_user_id"] == uid:
            entry["notified"] = _stamp()
    save(entries)


def notify_admin(client, entry: dict) -> bool:
    """DM the admin about one request. Never raises; returns whether it went out.

    Best-effort by design: a failed notification must not break the refusal or the
    answer the user is waiting on. The request stays queued either way, and
    ``aspen-users requests`` shows it regardless of whether Slack cooperated.
    """
    admin = config.ADMIN_USER_ID
    if not admin or client is None:
        return False
    if not due_for_notice(entry):
        return False

    text = (
        f"*Aspen request* — {describe(entry)}\n"
        f"```\n{command_for(entry)}\n```\n"
        "_Run it if you agree; ignore it if you don't. "
        "`./aspen-users requests` lists everything waiting._"
    )
    try:
        channel = admin
        try:
            # Opening the IM is the documented path and needs `im:write`; posting
            # straight to the user ID works once a DM exists, so it is the fallback
            # for workspaces where that scope was not granted.
            channel = client.conversations_open(users=admin)["channel"]["id"]
        except Exception:
            log.debug("conversations_open failed; posting to the user ID directly",
                      exc_info=True)
        client.chat_postMessage(channel=channel, text=text)
    except Exception:
        log.warning("Could not DM the admin about a %s request from %s",
                    entry["kind"], entry["slack_user_id"], exc_info=True)
        return False
    mark_notified(entry["kind"], entry["slack_user_id"])
    return True


def raise_request(kind: str, uid: str, client=None, display_name: str = "",
                  detail: str = "") -> dict:
    """Record a request and tell the admin, in one call."""
    entry = record(kind, uid, display_name=display_name, detail=detail)
    notify_admin(client, entry)
    return entry
