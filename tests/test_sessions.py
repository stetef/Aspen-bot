"""Tests for the per-thread session key and the pre-warmed session pool.

Conversation context is retained inside each warm SDK session (the SessionManager
keeps one per thread), so there is no separate history store to test here.
"""

import asyncio

import pytest


def test_thread_key_prefers_thread_ts(sut):
    assert sut._thread_key({"channel": "C", "thread_ts": "1", "ts": "2"}) == "C:1"


def test_thread_key_falls_back_to_ts(sut):
    assert sut._thread_key({"channel": "C", "ts": "2"}) == "C:2"


def test_thread_key_handles_missing_fields(sut):
    assert sut._thread_key({}) == ":"


# --------------------------------------------------------------------------- #
# Pre-warmed session pool
#
# Connecting an SDK session spawns the Claude Code CLI and waits on its init
# handshake (~1.7 s), which otherwise lands inside the first message of every new
# Slack thread. The pool moves that cost off the user's critical path.
# --------------------------------------------------------------------------- #
class _FakeSession:
    """SdkSession stand-in — records connects without spawning a CLI."""

    def __init__(self, key):
        self.key = key
        self.connects = 0
        self.closed = 0

    async def _ensure(self):
        self.connects += 1

    async def aclose(self):
        self.closed += 1


@pytest.fixture
def fake_sessions(sut, monkeypatch):
    """Patch the lazily-imported SdkSession and hand back everything created."""
    import aspen.agent

    made = []

    def _factory(key):
        s = _FakeSession(key)
        made.append(s)
        return s

    monkeypatch.setattr(aspen.agent, "SdkSession", _factory)
    return made


async def _drain():
    """Let the manager's background warm tasks finish."""
    for _ in range(10):
        await asyncio.sleep(0)


def test_warm_connects_spares_up_front(sut, fake_sessions, monkeypatch):
    from aspen import config

    monkeypatch.setattr(config, "PREWARM_SESSIONS", 2)

    async def go():
        sut.MANAGER.warm()
        await _drain()

    asyncio.run(go())

    assert len(fake_sessions) == 2
    assert all(s.connects == 1 for s in fake_sessions)   # connected before any message


def test_new_thread_adopts_a_spare_instead_of_connecting(sut, fake_sessions, monkeypatch):
    from aspen import config

    monkeypatch.setattr(config, "PREWARM_SESSIONS", 1)

    async def go():
        sut.MANAGER.warm()
        await _drain()
        entry = await sut.MANAGER._get_or_create("C:1")
        await _drain()
        return entry

    entry = asyncio.run(go())

    spare = fake_sessions[0]
    assert entry.session is spare        # the turn got the already-connected client
    assert spare.key == "C:1"            # and it was rekeyed to the adopting thread
    assert len(fake_sessions) == 2       # a replacement spare was warmed behind it


def test_prewarm_can_be_disabled(sut, fake_sessions, monkeypatch):
    from aspen import config

    monkeypatch.setattr(config, "PREWARM_SESSIONS", 0)

    async def go():
        sut.MANAGER.warm()
        await _drain()
        entry = await sut.MANAGER._get_or_create("C:1")
        await _drain()
        return entry

    entry = asyncio.run(go())

    # Exactly one session, created for the thread itself — no spares anywhere.
    assert len(fake_sessions) == 1
    assert entry.session.key == "C:1"
    assert sut.MANAGER._spares == []


def test_spares_respect_the_open_session_cap(sut, fake_sessions, monkeypatch):
    """Spares are live CLI subprocesses, so they count against MAX_OPEN_SESSIONS."""
    from aspen import config

    monkeypatch.setattr(config, "PREWARM_SESSIONS", 3)
    monkeypatch.setattr(config, "MAX_OPEN_SESSIONS", 2)

    async def go():
        await sut.MANAGER._get_or_create("C:1")   # 1 entry, leaving room for 1 spare
        await _drain()

    asyncio.run(go())

    assert len(sut.MANAGER._spares) == 1


def test_failed_prewarm_is_not_fatal(sut, monkeypatch):
    """A spare that can't connect must not break the next turn — it just creates
    its own session, where the real error surfaces with the normal handling."""
    import aspen.agent
    from aspen import config

    monkeypatch.setattr(config, "PREWARM_SESSIONS", 1)

    made = []

    class _Broken(_FakeSession):
        def __init__(self, key):
            super().__init__(key)
            made.append(self)

        async def _ensure(self):
            raise RuntimeError("CLI missing")

    monkeypatch.setattr(aspen.agent, "SdkSession", _Broken)

    async def go():
        sut.MANAGER.warm()
        await _drain()
        assert sut.MANAGER._spares == []          # nothing broken got pooled
        assert made[0].closed == 1                # and the dud was disposed of
        entry = await sut.MANAGER._get_or_create("C:1")
        await _drain()
        return entry

    entry = asyncio.run(go())

    assert entry.session.key == "C:1"             # the turn still gets a session
