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
    """Keeps one parked SdkSession per conversation thread (lives on the loop).

    Also keeps up to ``config.PREWARM_SESSIONS`` already-connected spares on
    standby, so a brand-new Slack thread doesn't pay the CLI connect handshake
    inside the user's first message.
    """

    def __init__(self):
        self._entries: "OrderedDict[str, _Entry]" = OrderedDict()
        self._spares: list = []            # connected SdkSessions awaiting a thread
        self._warming = 0                  # warm tasks in flight (spares not yet ready)

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
            entry = _Entry(self._claim_session(key))
            self._entries[key] = entry
            self.warm()                    # top the pool back up in the background
        return entry

    # --- pre-warming ------------------------------------------------------- #
    def _claim_session(self, key: str):
        """A session for ``key`` — a pre-connected spare if one is ready, else new.

        A demo thread never adopts a spare: spares are built with the ordinary
        options, and a demo session must be created without the Bash allowlist so
        the Slurm clients cannot reach the real cluster. Costs that one turn the
        warm-start saving, which is the right trade for a control that actually
        holds. The demo session exists before the turn runs (the Slack front-end
        starts it ahead of the allowlist gate), so the key alone is enough to
        know.
        """
        from . import demo
        from .agent import SdkSession
        if demo.get(key) is not None:
            log.debug("Demo thread %s — fresh session with Bash disabled", key)
            return SdkSession(key, allow_bash=False)
        if self._spares:
            session = self._spares.pop()
            session.key = key              # spares are keyless until adopted
            log.debug("Adopted pre-warmed session for %s", key)
            return session
        return SdkSession(key)

    def warm(self) -> None:
        """Top the spare pool back up, connecting in the background (non-blocking).

        Safe to call from anywhere on the loop; call it once at startup so the
        very first message finds a spare waiting.
        """
        while self._pool_shortfall() > 0:
            self._warming += 1
            asyncio.get_running_loop().create_task(self._warm_one())

    def _pool_shortfall(self) -> int:
        """How many more spares to start, respecting the open-session cap."""
        want = min(
            config.PREWARM_SESSIONS,
            config.MAX_OPEN_SESSIONS - len(self._entries),   # spares count against the cap
        )
        return want - len(self._spares) - self._warming

    async def _warm_one(self) -> None:
        from .agent import SdkSession
        session = SdkSession("(spare)")
        try:
            await session._ensure()        # spawns + connects the CLI subprocess
        except Exception:
            # A spare that won't connect is not fatal — the next turn just creates
            # its own session and surfaces the real error there.
            log.warning("Pre-warm failed; continuing without a spare", exc_info=True)
            await session.aclose()
        else:
            self._spares.append(session)
        finally:
            self._warming -= 1

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

    def has_session(self, key: str) -> bool:
        """True if a live (non-evicted) session already exists for this thread.

        Read from the Slack (Bolt) thread to decide whether an un-mentioned
        group-DM reply belongs to a thread Aspen already joined. A plain dict
        membership test — GIL-atomic — with only a benign race: an eviction right
        after this returns True just makes the next turn start a fresh session.
        """
        return key in self._entries

    def clear(self) -> None:
        """Drop all sessions and spares (used by tests; production relies on eviction)."""
        self._entries.clear()
        self._spares.clear()


MANAGER = SessionManager()
