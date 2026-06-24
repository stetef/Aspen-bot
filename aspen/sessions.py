"""
Conversation sessions and the persistent agent event loop.

This module owns two things:

1. The per-thread conversation **history store** (``_histories`` + the
   ``_thread_key`` / ``_get_history`` / ``_append_history`` helpers) — unchanged
   in behavior from the original single file. The Messages backend reads/writes
   history through these.

2. The **session registry + persistent asyncio loop**. Slack's (sync) handlers
   feed each user message into this async system via ``run_coroutine_threadsafe``;
   the ``SessionManager`` keeps one ``AgentSession`` per conversation thread, which
   runs a turn and then parks until the next message. Per-session locks serialize
   turns in the same thread; idle / LRU eviction (``aclose``) bounds live sessions.
"""

import asyncio
import logging
import threading
import time
from collections import OrderedDict

from . import config

log = logging.getLogger("aspen")


# --------------------------------------------------------------------------- #
# Conversation history store (behavior-preserving)
# --------------------------------------------------------------------------- #
_history_lock = threading.Lock()
_histories: dict[str, dict] = {}       # key → {"turns": [...], "last_ts": float}


def _thread_key(event: dict) -> str:
    ts = event.get("thread_ts") or event.get("ts", "")
    return f"{event.get('channel', '')}:{ts}"


def _get_history(key: str) -> list[dict]:
    with _history_lock:
        entry = _histories.get(key)
        if not entry:
            return []
        if time.time() - entry["last_ts"] > config.CONTEXT_EXPIRY:
            del _histories[key]
            return []
        return list(entry["turns"])


def _append_history(key: str, user_msg: str, assistant_msg: str) -> None:
    with _history_lock:
        entry = _histories.setdefault(key, {"turns": [], "last_ts": 0.0})
        entry["turns"].extend([
            {"role": "user",      "content": user_msg},
            {"role": "assistant", "content": assistant_msg},
        ])
        entry["turns"] = entry["turns"][-config.CONTEXT_MAX_TURNS:]
        entry["last_ts"] = time.time()


# --------------------------------------------------------------------------- #
# Persistent agent event loop
# --------------------------------------------------------------------------- #
_LOOP: asyncio.AbstractEventLoop | None = None
_LOOP_LOCK = threading.Lock()


def _ensure_loop() -> asyncio.AbstractEventLoop:
    """Start (once) and return the background asyncio loop that owns all sessions."""
    global _LOOP
    with _LOOP_LOCK:
        if _LOOP is None:
            _LOOP = asyncio.new_event_loop()
            threading.Thread(
                target=_LOOP.run_forever, name="aspen-agent-loop", daemon=True
            ).start()
    return _LOOP


# --------------------------------------------------------------------------- #
# Session registry
# --------------------------------------------------------------------------- #
class _Entry:
    __slots__ = ("session", "lock", "last_used")

    def __init__(self, session):
        self.session = session
        self.lock = asyncio.Lock()
        self.last_used = time.time()


class SessionManager:
    """Keeps one parked AgentSession per conversation thread (lives on the loop)."""

    def __init__(self):
        self._entries: "OrderedDict[str, _Entry]" = OrderedDict()

    async def handle(self, key: str, user_message: str, context: dict) -> tuple[str, list[str]]:
        entry = await self._get_or_create(key)
        async with entry.lock:   # serialize turns within one thread
            entry.last_used = time.time()
            self._entries.move_to_end(key)
            return await entry.session.send(user_message, context)

    async def _get_or_create(self, key: str) -> _Entry:
        await self._evict()
        entry = self._entries.get(key)
        if entry is None:
            from .backends import make_session
            entry = _Entry(make_session(config.ASPEN_BACKEND, key))
            self._entries[key] = entry
        return entry

    async def _evict(self) -> None:
        now = time.time()
        # Idle sessions past the context-expiry window.
        for k in [k for k, e in self._entries.items()
                  if now - e.last_used > config.CONTEXT_EXPIRY]:
            await self._entries.pop(k).session.aclose()
        # LRU overflow beyond the open-session cap.
        while len(self._entries) > config.MAX_OPEN_SESSIONS:
            _, e = self._entries.popitem(last=False)
            await e.session.aclose()

    def clear(self) -> None:
        """Drop all sessions (used by tests; production relies on eviction)."""
        self._entries.clear()


MANAGER = SessionManager()
