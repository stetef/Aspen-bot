"""
Turn telemetry — what people ask Aspen, and how well it answers.

One JSON line per turn, appended to ``<telemetry dir>/YYYYMMDD.jsonl``. The point
is to find the *tasks* people actually bring to Aspen so the tool surface, prompt
and workflows can be tuned for them — not to watch individuals.

Two switches, deliberately separate, because they have different lifetimes:

* **metrics** — who, when, which tools, latency, outcome, cost. Small,
  non-sensitive, and still worth having a year from now (failure rates, allowlist
  gaps, cost per user). Leave it on.
* **content** — the text of the question. That is what you need to build a task
  taxonomy, and what you can switch off once you have one. Time-boxable through
  ``content_until``, so a collection window closes by itself instead of relying on
  someone remembering to close it.

Turning content off — globally, or per person via ``excluded_users`` — still
writes the record, with ``text: null``, ``redacted: true``, and the character
count kept, so the volume/latency/outcome series stay unbroken.

Layering, strongest first:

1. ``ASPEN_TELEMETRY=false`` in the environment — an operator kill switch nothing
   else can override, and the fallback when the state file is unreadable.
2. ``<state dir>/telemetry.json`` — written only by ``aspen-users telemetry``,
   hot-reloaded on mtime like the registry, so a change lands on the next turn
   rather than at the next restart.
3. Defaults (everything on) when no state file exists.

The bot only ever *reads* the state file. Both it and the log live under
``ASPEN_STATE_DIR`` — outside the workspace and outside every sandbox-writable
path, enforced at startup by ``main._check_state_locations`` — so generated
analysis code can neither read the log nor switch off its own recording.

Nothing here may break a turn: ``record`` swallows and logs every failure.
"""

import json
import logging
import os
import threading
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from . import config, registry

log = logging.getLogger("aspen")

# Cache: (mtime_ns, size) of the state file we parsed -> parsed payload.
_CACHE: dict = {"stamp": None, "data": None}
# Appends are small enough to be atomic under O_APPEND, but turns land on several
# Bolt listener threads at once; the lock keeps that guarantee explicit and free.
_WRITE_LOCK = threading.Lock()

_DEFAULTS = {"metrics": True, "content": True, "content_until": "", "excluded_users": []}

# Recorded outcomes. The rejection paths matter as much as the successful ones:
# they are the demand Aspen is currently refusing.
OUTCOMES = (
    "ok",                  # a turn ran and produced a reply
    "empty",               # a mention with no actual question
    "error",               # the turn raised (see result_subtype for detail)
    "timeout",             # the turn exceeded _TURN_TIMEOUT
    "not_authorized",      # sender is not on the allowlist
    "group_gate",          # a group DM containing someone unapproved
    "rate_limited",        # per-user rate limit or an already-running turn
    "busy",                # global concurrency cap
)
# Outcomes whose text is never recorded, whatever the settings say: these are
# people who have not agreed to use Aspen (or a room it declined to join).
_NEVER_CONTENT = ("not_authorized", "group_gate")


def today() -> date:
    """Today in UTC.

    One clock for the whole module: the daily filenames, the content window, and
    pruning all have to agree, or a window closes on the wrong side of midnight
    and ``prune`` measures a cutoff against dates that were stamped differently.
    Timestamps in the records are UTC too, so the log has a single timeline.
    """
    return datetime.now(timezone.utc).date()


# --------------------------------------------------------------------------- #
# Settings
# --------------------------------------------------------------------------- #
def _normalize(raw: dict) -> dict:
    """Validate a parsed state payload, falling back per-field rather than whole."""
    settings = dict(_DEFAULTS)
    for flag in ("metrics", "content"):
        if isinstance(raw.get(flag), bool):
            settings[flag] = raw[flag]
    until = str(raw.get("content_until") or "").strip()
    settings["content_until"] = until
    excluded = raw.get("excluded_users")
    if isinstance(excluded, list):
        settings["excluded_users"] = sorted(
            {str(u).strip() for u in excluded if str(u).strip()}
        )
    settings["updated"] = str(raw.get("updated") or "")
    settings["updated_by"] = str(raw.get("updated_by") or "")
    return settings


def load(force: bool = False) -> dict:
    """The on-disk settings, re-read whenever the file changes.

    A missing file means "never configured" — defaults apply. An unreadable one
    keeps the last good copy, and falls back to defaults only if we never had one;
    both directions are deliberate, since telemetry failing open costs nothing but
    a log line while failing closed silently loses the data you asked for.
    """
    path = config.TELEMETRY_STATE_FILE
    try:
        st = path.stat()
        stamp = (st.st_mtime_ns, st.st_size)
    except OSError:
        if _CACHE["stamp"] != "missing":
            _CACHE.update(stamp="missing", data=dict(_DEFAULTS, source="default"))
        return _CACHE["data"]

    if not force and _CACHE["stamp"] == stamp and _CACHE["data"] is not None:
        return _CACHE["data"]

    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = dict(_normalize(json.load(fh)), source="file")
    except Exception:
        log.exception("telemetry: could not read %s", path)
        if _CACHE["data"] is not None:
            return _CACHE["data"]
        return dict(_DEFAULTS, source="default")

    _CACHE.update(stamp=stamp, data=data)
    return data


def invalidate() -> None:
    """Drop the cached settings (used by the CLI after a write, and by tests)."""
    _CACHE.update(stamp=None, data=None)


def _content_window_open(until: str, on: Optional[date] = None) -> tuple[bool, str]:
    """Is the content window still open? Returns (open, reason-if-closed).

    ``content_until`` is inclusive — the last day of collection. An unparseable
    date closes the window: a typo must not silently mean "collect forever".
    """
    if not until:
        return True, ""
    try:
        deadline = date.fromisoformat(until)
    except ValueError:
        log.warning("telemetry: content_until %r is not a YYYY-MM-DD date — "
                    "treating the window as closed", until)
        return False, f"content_until {until!r} is not a valid date"
    if (on or today()) > deadline:
        return False, f"the collection window closed on {until}"
    return True, ""


def effective(on: Optional[date] = None) -> dict:
    """Settings as they actually apply right now (env floor + expiry applied)."""
    if not config.TELEMETRY_ENABLED:
        return {"metrics": False, "content": False, "content_until": "",
                "excluded_users": [], "source": "env",
                "off_reason": "ASPEN_TELEMETRY is off in the environment"}

    settings = load()
    window_open, closed_reason = _content_window_open(settings["content_until"], on)
    content = settings["content"] and window_open
    if not settings["content"]:
        closed_reason = "content collection is switched off"
    return {
        "metrics": settings["metrics"],
        "content": content,
        "content_until": settings["content_until"],
        "excluded_users": list(settings["excluded_users"]),
        "source": settings.get("source", "default"),
        "updated": settings.get("updated", ""),
        "updated_by": settings.get("updated_by", ""),
        "off_reason": "" if content else closed_reason,
    }


def save(settings: dict, actor: str = "") -> dict:
    """Atomically write the state file (CLI only) and drop the cache."""
    path = config.TELEMETRY_STATE_FILE
    registry.ensure_private_dir(path.parent)
    payload = dict(
        _normalize(settings),
        updated=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        updated_by=actor,
    )
    tmp = path.with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2, ensure_ascii=False)
        fh.write("\n")
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)
    invalidate()
    return payload


# --------------------------------------------------------------------------- #
# Recording
# --------------------------------------------------------------------------- #
def _log_path(on: Optional[date] = None) -> Path:
    return config.TELEMETRY_DIR / f"{(on or today()):%Y%m%d}.jsonl"


def _short_tools(tools: Optional[list]) -> list[str]:
    """Tool names without the MCP prefix, in call order.

    Order is the point: a sequence that keeps repeating across turns is a
    candidate for one purpose-built tool.
    """
    return [str(t).removeprefix("mcp__aspen__") for t in (tools or [])]


def _flatten_meta(meta: Optional[dict]) -> dict:
    """The fields worth keeping from the SDK's ResultMessage (see agent.send)."""
    meta = meta or {}
    keep = ("result_subtype", "num_turns", "denials", "input_tokens",
            "output_tokens", "cost_usd", "agent_ms")
    return {k: meta[k] for k in keep if meta.get(k) is not None}


def record(uid: str, outcome: str, text: str = "", channel: str = "",
           thread: str = "", latency_ms: Optional[int] = None,
           tools: Optional[list] = None, reply_chars: int = 0,
           attachments: int = 0, meta: Optional[dict] = None) -> None:
    """Append one turn record. Never raises — telemetry must not cost a turn."""
    try:
        _record(uid, outcome, text, channel, thread, latency_ms, tools,
                reply_chars, attachments, meta)
    except Exception:
        log.debug("telemetry: could not record a turn", exc_info=True)


def _record(uid, outcome, text, channel, thread, latency_ms, tools,
            reply_chars, attachments, meta) -> None:
    settings = effective()
    if not settings["metrics"]:
        return

    text = text or ""
    keep_text = (
        settings["content"]
        and outcome not in _NEVER_CONTENT
        and uid not in settings["excluded_users"]
    )
    entry = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "uid": uid,
        "alias": (registry.by_id(uid) or {}).get("alias", ""),
        "outcome": outcome,
        "channel": channel,
        "thread": thread,
        "latency_ms": latency_ms,
        "tools": _short_tools(tools),
        # Kept even when the text is not: length alone separates a one-line
        # status check from a pasted traceback, and is not sensitive.
        "chars": len(text),
        "reply_chars": reply_chars,
        "attachments": attachments,
        **_flatten_meta(meta),
    }
    if keep_text:
        entry["text"] = text[: config.TELEMETRY_MAX_TEXT]
    else:
        entry["text"] = None
        entry["redacted"] = True

    path = _log_path()
    registry.ensure_private_dir(path.parent)
    with _WRITE_LOCK:
        is_new = not path.exists()
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
        if is_new:
            os.chmod(path, 0o600)


# --------------------------------------------------------------------------- #
# Reading back (the CLI and any offline analysis)
# --------------------------------------------------------------------------- #
def log_files() -> list[Path]:
    """Every daily log, oldest first."""
    try:
        return sorted(p for p in config.TELEMETRY_DIR.glob("*.jsonl") if p.is_file())
    except OSError:
        return []


def read_all() -> list[dict]:
    """Every recorded turn, oldest first. Malformed lines are skipped."""
    entries = []
    for path in log_files():
        try:
            with open(path, "r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entries.append(json.loads(line))
                    except ValueError:
                        log.warning("telemetry: skipping a malformed line in %s", path)
        except OSError:
            log.warning("telemetry: could not read %s", path)
    return entries


def prune(days: int, on: Optional[date] = None) -> list[str]:
    """Delete daily logs older than ``days``. Returns the filenames removed."""
    cutoff = (on or today()) - timedelta(days=days)
    removed = []
    for path in log_files():
        try:
            stamp = datetime.strptime(path.stem, "%Y%m%d").date()
        except ValueError:
            continue                      # not one of ours; leave it alone
        if stamp < cutoff:
            path.unlink()
            removed.append(path.name)
    return removed
