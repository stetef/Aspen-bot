"""
User registry — Slack ID ↔ alias/display name, and the admission allowlist.

**The registry is the source of truth for who may talk to Aspen.** It is a single
JSON file (``config.USERS_FILE``) that the ``aspen-users`` CLI writes and the bot
only ever *reads*. Admission changes deliberately live outside the agent's reach:
no tool, prompt, or Slack message can add or remove a user, so a prompt injection
can never widen the allowlist.

Two properties matter throughout:

* **Resolve by ID, display by alias.** ``slack_user_id`` is the durable key;
  ``alias`` is a human-friendly kebab-case label for folder names and CLI
  arguments. An alias can be renamed at any time without breaking a lookup, and a
  lookup by alias only ever *finds* a user — it never authorizes one (that is
  always ``slack_user_id``).
* **Hot reload.** The file is re-read whenever its mtime/size changes, so
  ``aspen-users remove`` takes effect on the target's *next message* rather than
  at the next restart. See ``config.__getattr__``, which routes
  ``config.ALLOWED_USER_IDS`` / ``config.ADMIN_USER_ID`` through here.

Failure behavior is deliberately "never widen": a malformed file keeps the last
good copy in memory (access is unchanged, the error is logged loudly). Only when
no good copy was ever loaded do we fall back to bootstrapping from
``ASPEN_ALLOWED_SLACK_USER_IDS`` — that env var is operator-controlled, so the
fallback can't grant more than the operator already configured, and it keeps a
corrupt file from locking everyone (including the admin) out.
"""

import json
import logging
import os
import re
import unicodedata
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Optional

from . import config

log = logging.getLogger("aspen")

# Alias grammar: lowercase kebab-case. Deliberately cannot start with "_", which
# is what keeps the reserved directory names (_group, _archive) unclaimable.
ALIAS_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
ALIAS_MAX_LEN = 32
# Slack member IDs: U… for people, W… on some Enterprise Grid workspaces. Real
# ones are ~9-11 chars, but this is only a typo guard (catching an alias or an
# @handle pasted where an ID belongs) — never a security control. Admission is an
# exact match against the ID Slack itself put on the event, so a loose pattern
# here costs nothing and keeps short fixture IDs usable in tests.
USER_ID_RE = re.compile(r"^[UW][A-Z0-9]+$")

RESERVED_ALIASES = {"group", "archive", "all", "none", "aspen", "admin"}

ROLES = ("admin", "member")
STATUSES = ("active", "removed")

# Cache: (mtime_ns, size) of the file we parsed -> parsed payload.
_CACHE: dict = {"stamp": None, "data": None}


# --------------------------------------------------------------------------- #
# Aliases
# --------------------------------------------------------------------------- #
def slugify(name: str) -> str:
    """Best-effort kebab-case alias from a display name ('Arun N.' -> 'arun-n')."""
    decomposed = unicodedata.normalize("NFKD", name or "")
    ascii_only = decomposed.encode("ascii", "ignore").decode("ascii")
    slug = re.sub(r"[^a-z0-9]+", "-", ascii_only.lower()).strip("-")
    return slug[:ALIAS_MAX_LEN].strip("-")


def display_from_alias(alias: str) -> str:
    """The readable name an alias stands for ('macon-abernathy' -> 'Macon Abernathy').

    Roughly the inverse of ``slugify``, and the reason it exists is that Slack's
    two name fields disagree: the alias is slugified from someone's real name
    while ``display_name`` took their Slack handle, so the registry ended up
    holding "macon-abernathy" next to "mjabern" and "arun-asundi" next to
    "arunasundi". A handle is not a name, and the near-miss between the two is
    what an agent guesses wrong on.

    Not a general title-caser: it only upper-cases the first letter of each
    hyphen-separated part, so "o'brien" and "mcdonald" keep the rest of their
    spelling rather than being mangled into "O'Brien" or "McDonald" by rule.
    """
    parts = [p for p in (alias or "").split("-") if p]
    return " ".join(p[:1].upper() + p[1:] for p in parts)


def validate_alias(alias: str) -> Optional[str]:
    """Return an error message if ``alias`` is unusable, else ``None``."""
    if not alias:
        return "alias is empty"
    if len(alias) > ALIAS_MAX_LEN:
        return f"alias is longer than {ALIAS_MAX_LEN} characters"
    if not ALIAS_RE.match(alias):
        return (
            f"'{alias}' is not a valid alias — use lowercase letters, digits and "
            "single hyphens (e.g. 'arun-n')"
        )
    if alias in RESERVED_ALIASES:
        return f"'{alias}' is reserved"
    return None


def validate_user_id(uid: str) -> Optional[str]:
    if not uid:
        return "Slack user ID is empty"
    if not USER_ID_RE.match(uid):
        return f"'{uid}' does not look like a Slack member ID (expected e.g. U01ABC2DEF)"
    return None


# --------------------------------------------------------------------------- #
# Loading
# --------------------------------------------------------------------------- #
def _bootstrap_from_env() -> dict:
    """Synthesize a registry from ``ASPEN_ALLOWED_SLACK_USER_IDS``.

    Used before the registry file exists (fresh install) and as the last-resort
    fallback when it can't be parsed and nothing good was ever cached.
    """
    ids = list(config.BOOTSTRAP_USER_IDS)
    return {
        "version": 1,
        "source": "env",
        "users": [
            {
                "slack_user_id": uid,
                "alias": uid.lower(),
                "display_name": uid,
                "role": "member",
                "status": "active",
            }
            for uid in ids
        ],
    }


def _normalize(raw: dict) -> dict:
    """Validate and normalize a parsed registry payload.

    Individual bad entries are dropped with a warning rather than failing the
    whole file — one malformed record shouldn't deauthorize the entire group.
    """
    users, seen_ids, seen_aliases = [], set(), set()
    for entry in raw.get("users") or []:
        if not isinstance(entry, dict):
            log.warning("registry: skipping non-object entry %r", entry)
            continue
        uid = str(entry.get("slack_user_id", "")).strip()
        if validate_user_id(uid):
            log.warning("registry: skipping entry with bad slack_user_id %r", uid)
            continue
        if uid in seen_ids:
            log.warning("registry: duplicate slack_user_id %s — keeping the first", uid)
            continue

        alias = str(entry.get("alias", "")).strip().lower()
        if validate_alias(alias) or alias in seen_aliases:
            fallback = uid.lower()
            log.warning(
                "registry: unusable/duplicate alias %r for %s — falling back to %r",
                alias, uid, fallback,
            )
            alias = fallback
        seen_ids.add(uid)
        seen_aliases.add(alias)

        role = str(entry.get("role", "member")).strip().lower()
        status = str(entry.get("status", "active")).strip().lower()
        users.append(
            {
                "slack_user_id": uid,
                "alias": alias,
                "display_name": str(entry.get("display_name") or alias),
                "role": role if role in ROLES else "member",
                "status": status if status in STATUSES else "active",
                "added": str(entry.get("added") or ""),
                "added_by": str(entry.get("added_by") or ""),
                "removed": str(entry.get("removed") or ""),
                "removed_by": str(entry.get("removed_by") or ""),
                "notes": str(entry.get("notes") or ""),
                # Where this person's calculations live. Empty = the shared
                # CALCULATIONS_ROOT default, which is what makes a deployment
                # that never sets one behave exactly as it did before. Validated
                # by `aspen-users set-root`; see roots.py.
                "calc_root": str(entry.get("calc_root") or ""),
                # Their Unix account on the cluster. A Slack ID does not name
                # one, and job submission on someone's behalf needs it.
                "unix_user": str(entry.get("unix_user") or ""),
                # Whether Aspen pings them when their jobs finish: "always",
                # "never", or empty = not asked yet. Agent-writable like
                # `declined`, because it only decides whether Aspen speaks to
                # this person about their own jobs. See notify.py.
                "notify_jobs": str(entry.get("notify_jobs") or ""),
                # Which registered runner submits their jobs (see runners.py).
                # Empty = they cannot submit. CLI-only, like calc_root: a tool
                # that wrote this would choose what executes, which is exactly
                # the surface the design keeps out of the model's reach.
                "job_runner": str(entry.get("job_runner") or ""),
                # Setup offers this person has turned down: {item: YYYY-MM-DD}.
                # Written by the agent (setup.decline) because it can only ever
                # make Aspen quieter — it grants nothing. See setup.py.
                "declined": {
                    str(k): str(v)
                    for k, v in (entry.get("declined") or {}).items()
                    if isinstance(entry.get("declined"), dict) and v
                },
            }
        )
    return {"version": int(raw.get("version", 1) or 1), "source": "file", "users": users}


def load(force: bool = False) -> dict:
    """Return the current registry, re-reading the file when it has changed."""
    path = config.USERS_FILE
    try:
        st = path.stat()
        stamp = (st.st_mtime_ns, st.st_size)
    except OSError:
        # No registry file yet — bootstrap mode. Cache it so a fresh install
        # doesn't stat-and-miss on every single turn.
        if _CACHE["stamp"] != "missing":
            log.info("No user registry at %s — using ASPEN_ALLOWED_SLACK_USER_IDS", path)
            _CACHE.update(stamp="missing", data=_bootstrap_from_env())
        return _CACHE["data"]

    if not force and _CACHE["stamp"] == stamp and _CACHE["data"] is not None:
        return _CACHE["data"]

    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = _normalize(json.load(fh))
    except Exception:
        # Never widen on failure: keep serving the last good copy if we have one.
        log.exception("registry: could not read %s", path)
        if _CACHE["data"] is not None:
            log.error("registry: keeping the last good copy (%d users)", len(_CACHE["data"]["users"]))
            return _CACHE["data"]
        log.error("registry: no cached copy — falling back to ASPEN_ALLOWED_SLACK_USER_IDS")
        return _bootstrap_from_env()

    _CACHE.update(stamp=stamp, data=data)
    return data


def invalidate() -> None:
    """Drop the cache (used by the CLI after a write, and by tests)."""
    _CACHE.update(stamp=None, data=None)


def ensure_private_dir(path: Path) -> None:
    """Create ``path`` (and parents) and make it ``0700``.

    Called at every creation point rather than only at bot startup: on a shared
    login node a registry or workflow tree that spends its first hours at 0755 is
    readable — and its parent traversable — by every other user on the node. The
    CLI usually creates these directories before the bot ever starts.
    """
    try:
        path.mkdir(parents=True, exist_ok=True)
        os.chmod(path, 0o700)
    except OSError:
        log.warning("Could not create/lock down %s — check its permissions by hand", path)


# --------------------------------------------------------------------------- #
# Queries
# --------------------------------------------------------------------------- #
def users(include_removed: bool = False) -> list[dict]:
    everyone = load()["users"]
    return everyone if include_removed else [u for u in everyone if u["status"] == "active"]


def by_id(uid: str, include_removed: bool = True) -> Optional[dict]:
    for u in users(include_removed=include_removed):
        if u["slack_user_id"] == uid:
            return u
    return None


def by_alias(alias: str, include_removed: bool = True) -> Optional[dict]:
    alias = (alias or "").strip().lower()
    for u in users(include_removed=include_removed):
        if u["alias"] == alias:
            return u
    return None


def resolve(token: str, include_removed: bool = True) -> Optional[dict]:
    """Find a user by Slack ID *or* alias.

    Lookup only — it never authorizes. Admission is always a
    ``slack_user_id in ALLOWED_USER_IDS`` check on the ID Slack itself supplied.
    """
    token = (token or "").strip()
    if not token:
        return None
    return (by_id(token, include_removed) or by_alias(token, include_removed)
            or by_alias(token.lstrip("@"), include_removed))


def allowed_ids() -> set[str]:
    """The admission allowlist — active users only."""
    return {u["slack_user_id"] for u in users()}


def admin_id() -> str:
    """Aspen's admin: the env override, else the first ``role: admin``, else the
    first active user (matching the historical "first ID in the list" rule)."""
    if config.ADMIN_OVERRIDE:
        return config.ADMIN_OVERRIDE
    active = users()
    for u in active:
        if u["role"] == "admin":
            return u["slack_user_id"]
    return active[0]["slack_user_id"] if active else ""


def label(uid: str) -> str:
    """Human label for a Slack ID — 'Arun N. (arun)', or the raw ID if unknown."""
    u = by_id(uid)
    if u is None:
        return uid or "unknown user"
    return f"{u['display_name']} ({u['alias']})"


def display_name(uid: str) -> str:
    u = by_id(uid)
    return u["display_name"] if u else ""


# --------------------------------------------------------------------------- #
# Writing (CLI only — the bot never calls this)
# --------------------------------------------------------------------------- #
def save(users_list: list[dict]) -> None:
    """Atomically write the registry and drop the cache.

    Only ``aspen-users`` calls this. The bot process has no code path that writes
    the registry, which is what keeps admission out of the agent's reach.
    """
    path = config.USERS_FILE
    ensure_private_dir(path.parent)
    payload = {
        "version": 1,
        "updated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "users": users_list,
    }
    tmp = path.with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2, ensure_ascii=False)
        fh.write("\n")
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)
    invalidate()


def new_user(uid: str, alias: str, display: str, role: str = "member",
             added_by: str = "", notes: str = "", calc_root: str = "",
             unix_user: str = "") -> dict:
    return {
        "slack_user_id": uid,
        "alias": alias,
        "display_name": display or alias,
        "role": role if role in ROLES else "member",
        "status": "active",
        "added": date.today().isoformat(),
        "added_by": added_by,
        "removed": "",
        "removed_by": "",
        "notes": notes,
        "calc_root": calc_root,
        "unix_user": unix_user,
        "declined": {},
    }
