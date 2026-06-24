"""
Conversation sessions and the persistent agent event loop.

The **session registry + persistent asyncio loop**. Slack's (sync) handlers feed
each user message into this async system via ``run_coroutine_threadsafe``; the
``SessionManager`` keeps one ``SdkSession`` per conversation thread, which runs a
turn and then parks until the next message (the warm SDK client retains the
conversation context). Per-session locks serialize turns in the same thread; idle /
LRU eviction (``aclose``) bounds live sessions. ``_thread_key`` maps a Slack thread
to its session.
"""

import asyncio
import logging
import threading
import time
from collections import OrderedDict

from . import config

log = logging.getLogger("aspen")


# --------------------------------------------------------------------------- #
# Thread key
# --------------------------------------------------------------------------- #
def _thread_key(event: dict) -> str:
    # Conversation context itself is retained inside each warm SDK session; this
    # key just maps a Slack thread to its session in the SessionManager.
    ts = event.get("thread_ts") or event.get("ts", "")
    return f"{event.get('channel', '')}:{ts}"


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
    """Keeps one parked SdkSession per conversation thread (lives on the loop)."""

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
            from .agent import SdkSession
            entry = _Entry(SdkSession(key))
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
