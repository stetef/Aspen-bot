"""
Reading conversations back.

The turn log records ``reply_chars`` but never the reply text (see
``aspen.telemetry``), so it cannot answer "what did Aspen actually say?". The
answer exists elsewhere: the SDK spawns the Claude Code CLI, which writes a full
transcript per session — one session per Slack thread — to
``~/.claude/projects/<slugified cwd>/<session id>.jsonl``. That file holds both
sides of every turn plus each tool call.

**This module is the one place that knows that file format.** Like ``data.py``
it reads on its own rather than importing anything from ``aspen``: that package
needs SLACK_BOT_TOKEN and friends at import time, and a read-only web app should
not carry the bot's runtime or its secrets.

Two things make the reader defensive rather than direct:

* **Which directory.** The project slug derives from the bot's *cwd*, which moves
  the day the sandbox is switched on. So scan every project directory and keep
  the sessions that carry Aspen's ``<aspen_context>`` preamble — that marker is
  also what separates the bot's sessions from the operator's own Claude Code
  sessions, which land in the same tree.
* **Which turns may be shown.** A transcript holds full text regardless of the
  telemetry content switch, so it is a way to walk straight around a redaction.
  Text is therefore gated on the turn log's own decision — see ``visible_turns``.

Nothing in this module writes.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

import pandas as pd

DEFAULT_PROJECTS_DIR = Path(
    os.getenv("CLAUDE_PROJECTS_DIR", str(Path.home() / ".claude" / "projects"))
)

# The preamble the bot prepends to every user turn. Its presence marks a session
# as Aspen's; the alias inside it says who was speaking on that turn.
_CONTEXT_END = "</aspen_context>"
_SPEAKER = re.compile(r"speaking to you right now is (.+?) \(alias `([^`]+)`")


# --------------------------------------------------------------------------- #
# Parsing one transcript
# --------------------------------------------------------------------------- #
def _blocks(message: dict | None) -> list[dict]:
    """Content blocks, normalising the plain-string form the CLI also emits."""
    content = (message or {}).get("content")
    if isinstance(content, list):
        return [b for b in content if isinstance(b, dict)]
    return [{"type": "text", "text": content or ""}]


def _text_of(message: dict | None) -> str:
    return "\n".join(b.get("text") or "" for b in _blocks(message)
                     if b.get("type") == "text")


def _strip_context(body: str) -> str:
    """Drop the injected preamble; what remains is what the person typed."""
    return body.split(_CONTEXT_END, 1)[-1].strip() if _CONTEXT_END in body else body.strip()


def read_session(path: Path) -> dict | None:
    """One transcript as ``{session_id, aliases, turns}``, or None if not Aspen's.

    A turn is one human message plus everything the agent did before the next
    one: its prose, and the names of the tools it called. Tool *results* are
    deliberately dropped — they are the bulk of the file and none of the
    conversation.
    """
    turns: list[dict] = []
    aliases: list[str] = []
    pending: dict | None = None
    try:
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except ValueError:
                    continue                # a torn write; skip the line
                kind = event.get("type")
                message = event.get("message")
                if kind == "user":
                    body = _text_of(message)
                    if _CONTEXT_END not in body:
                        continue            # a tool result, not a person
                    found = _SPEAKER.search(body)
                    alias = found.group(2) if found else "unknown"
                    if alias not in aliases:
                        aliases.append(alias)
                    if pending:
                        turns.append(pending)
                    pending = {"who": alias, "question": _strip_context(body),
                               "ts": event.get("timestamp", ""), "reply": [], "tools": []}
                elif kind == "assistant" and pending is not None:
                    for block in _blocks(message):
                        if block.get("type") == "text" and (block.get("text") or "").strip():
                            pending["reply"].append(block["text"].strip())
                        elif block.get("type") == "tool_use":
                            pending["tools"].append(
                                str(block.get("name", "?")).removeprefix("mcp__aspen__"))
    except OSError:
        return None
    if pending:
        turns.append(pending)
    if not aliases:
        return None                         # not one of Aspen's sessions
    return {"session_id": path.stem, "aliases": aliases, "turns": turns,
            "mtime": path.stat().st_mtime}


def read_all(projects_dir: Path) -> list[dict]:
    """Every Aspen session under every project directory, newest first."""
    sessions = []
    try:
        candidates = sorted(Path(projects_dir).glob("*/*.jsonl"))
    except OSError:
        return []
    for path in candidates:
        session = read_session(path)
        if session and session["turns"]:
            sessions.append(session)
    return sorted(sessions, key=lambda s: s["mtime"], reverse=True)


def stamp(projects_dir: Path) -> float:
    """Newest mtime under the projects tree — lets a cache refresh on a new turn."""
    try:
        return max((p.stat().st_mtime for p in Path(projects_dir).glob("*/*.jsonl")),
                   default=0.0)
    except OSError:
        return 0.0


# --------------------------------------------------------------------------- #
# What may be shown
# --------------------------------------------------------------------------- #
def title_of(session: dict, width: int = 70) -> str:
    """The first thing the person said, truncated — the Claude sidebar convention."""
    if not session["turns"]:
        return "(empty)"
    first = " ".join(session["turns"][0]["question"].split())
    return first[:width] + "…" if len(first) > width else first or "(no text)"


def index(sessions: list[dict]) -> pd.DataFrame:
    """One row per conversation, for listing and filtering."""
    rows = [{
        "session_id": s["session_id"],
        "who": s["aliases"][0] if len(s["aliases"]) == 1 else ", ".join(s["aliases"]),
        "title": title_of(s),
        "turns": len(s["turns"]),
        "started": s["turns"][0]["ts"][:19] if s["turns"] else "",
        "last": s["turns"][-1]["ts"][:19] if s["turns"] else "",
    } for s in sessions]
    frame = pd.DataFrame(rows)
    if frame.empty:
        return frame
    for col in ("started", "last"):
        frame[col] = pd.to_datetime(frame[col], errors="coerce", utc=True)
    return frame.sort_values("last", ascending=False)


def excluded_users(state_file: Path) -> set[str]:
    """Slack IDs currently opted out of content collection.

    Read straight from ``<state dir>/telemetry.json`` for the same reason the
    turn log is read straight: importing ``aspen.telemetry`` would drag in
    ``aspen.config`` and the bot's whole runtime. A missing or unreadable file
    means nothing is excluded, matching the bot's own default.
    """
    try:
        with open(state_file, "r", encoding="utf-8") as fh:
            raw = json.load(fh)
    except (OSError, ValueError):
        return set()
    listed = raw.get("excluded_users")
    return {str(u).strip() for u in listed if str(u).strip()} if isinstance(listed, list) else set()


def visible_turns(session: dict, turn_log: pd.DataFrame,
                  excluded: set[str] | None = None,
                  show_unjoined: bool = True) -> list[dict]:
    """The session's turns, with text withheld wherever the log withheld it.

    The gate is the turn log's own ``redacted`` flag rather than a re-derivation
    of the rules: the bot already applied the content switch, the collection
    window and the exclusion list at the moment of recording, so that flag *is*
    the per-turn decision, made with the settings that were in force. Re-deciding
    it here could only disagree with it.

    Two additions on top. A person on the exclusion list *now* has their past
    conversations withheld too, so removing someone takes effect backwards. And a
    turn with no matching log row — anything recorded before ``session_id`` was
    logged — is marked ``unjoined``.

    Unjoined turns are shown, because that is what the recording policy actually
    said: collection defaults to on (``telemetry._DEFAULTS``), so a turn with no
    row to consult was recorded under whatever was in force at the time, not
    under a prohibition. Treating "no record" as "withhold" would hide
    conversations collected under a fully open policy, while an explicit opt-out
    is still honoured by the two checks above. ``show_unjoined=False`` restores
    the strict reading for an operator who wants only turns they can verify.
    """
    excluded = excluded or set()
    joined = pd.DataFrame()
    if not turn_log.empty and "session_id" in turn_log:
        joined = turn_log[turn_log["session_id"] == session["session_id"]]

    out = []
    for turn in session["turns"]:
        row = _match(joined, turn["ts"])
        if row is None:
            out.append({**turn, "shown": show_unjoined, "unjoined": True,
                        "why": "" if show_unjoined else "no telemetry record"})
        elif bool(row.get("redacted")):
            out.append({**turn, "shown": False, "unjoined": False, "why": "recorded without text"})
        elif str(row.get("uid") or "") in excluded:
            out.append({**turn, "shown": False, "unjoined": False, "why": "person opted out"})
        else:
            out.append({**turn, "shown": True, "unjoined": False, "why": ""})
    return out


def _match(rows: pd.DataFrame, ts: str):
    """The log row for a turn, matched on session plus nearest timestamp.

    The session id narrows it to one thread; within a thread the turns are
    seconds to minutes apart while the two clocks agree to well under that, so
    nearest-in-time is unambiguous. A 5-minute guard keeps a turn whose partner
    was never recorded (the bot died mid-turn) from adopting a neighbour's row.
    """
    if rows.empty or not ts:
        return None
    when = pd.to_datetime(ts, errors="coerce", utc=True)
    if pd.isna(when):
        return None
    delta = (rows["ts"] - when).abs()
    nearest = delta.idxmin()
    if delta.loc[nearest] > pd.Timedelta(minutes=5):
        return None
    return rows.loc[nearest]
