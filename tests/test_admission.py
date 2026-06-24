"""
Characterization tests for the admission gates in ``_handle_event``:
allowlist -> per-user in-flight/rate limit -> global concurrency semaphore.

``_run_agent`` is stubbed so these tests stay hermetic (no LLM calls) and focus
purely on the gating and Slack-reply behavior.
"""

from unittest.mock import MagicMock

import pytest


@pytest.fixture
def no_op_agent(sut, monkeypatch):
    """Stub the session turn so admission tests stay hermetic (no real SDK)."""

    async def _fake_handle(key, user_message, context):
        return "reply!", []

    monkeypatch.setattr(sut.MANAGER, "handle", _fake_handle)


def _event(user, text, channel="C", ts="1.0"):
    return {"user": user, "text": text, "channel": channel, "ts": ts}


def test_unauthorized_user_is_rejected(sut, say):
    sut._handle_event(_event("UNKNOWN", "hello"), say, MagicMock(), strip_mention=False)
    assert any("not authorized" in t for t in say.texts)


def test_happy_path_strips_mention_and_replies(sut, say, no_op_agent):
    sut._handle_event(_event("U1", "<@BOT123> hello there"), say, MagicMock(), strip_mention=True)
    assert "_Thinking…_" in say.texts
    assert "reply!" in say.texts
    # The in-flight flag is released in the finally block.
    assert sut._user_active["U1"] is False


def test_empty_message_prompts_for_input(sut, say, no_op_agent):
    sut._handle_event(_event("U1", "<@BOT123>"), say, MagicMock(), strip_mention=True)
    assert any("Ask me anything" in t for t in say.texts)


def test_reentrant_user_gets_busy_message(sut, say, no_op_agent):
    # Simulate an already in-flight request for this user.
    sut._user_active["U1"] = True
    sut._handle_event(_event("U1", "another one"), say, MagicMock(), strip_mention=False)
    assert any("still working on your previous request" in t for t in say.texts)


def test_global_concurrency_cap_rejects_extra_user(sut, say, no_op_agent):
    # Saturate the global semaphore (MAX_CONCURRENT slots already in use).
    for _ in range(sut.MAX_CONCURRENT):
        assert sut._global_sem.acquire(blocking=False)

    # The (MAX_CONCURRENT + 1)th distinct user is turned away immediately.
    sut._handle_event(_event("U3", "let me in"), say, MagicMock(), strip_mention=False)

    assert any("busy right now" in t for t in say.texts)
    # Rejected, not queued: the user's in-flight flag is released ...
    assert sut._user_active["U3"] is False
    # ... but the existing quirk stands — the rejected attempt still consumed a
    # rate-limit slot (the timestamp is recorded before the semaphore check).
    assert len(sut._rate_data["U3"]) == 1
