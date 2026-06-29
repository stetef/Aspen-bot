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


def _event(user, text, channel="C", ts="1.0", channel_type="channel"):
    # channel_type defaults to a non-mpim value so the group-DM participant gate
    # short-circuits (no Slack API call) for these per-mentioner admission tests.
    return {"user": user, "text": text, "channel": channel, "ts": ts, "channel_type": channel_type}


def _group_event(user, text, channel="Gmpim1", ts="1.0", with_type=True):
    ev = {"user": user, "text": text, "channel": channel, "ts": ts}
    if with_type:
        ev["channel_type"] = "mpim"  # message-event style; app_mention omits it
    return ev


def _mpim_client(members, bot_uid="BOT123", bots=()):
    """A Slack client mock for a group DM with the given member IDs.

    ``bots`` lists member IDs that ``users.info`` should report as apps (is_bot).
    """
    client = MagicMock()
    client.auth_test.return_value = {"user_id": bot_uid}
    client.conversations_members.return_value = {"members": members}
    client.conversations_info.return_value = {"channel": {"is_mpim": True}}

    def _users_info(user):
        return {"user": {"id": user, "is_bot": user in bots,
                         "profile": {"display_name": f"name-{user}"}}}

    client.users_info.side_effect = _users_info
    return client


def test_unauthorized_user_is_rejected(sut, say):
    sut._handle_event(_event("UNKNOWN", "hello"), say, MagicMock(), strip_mention=False)
    assert any("not authorized" in t for t in say.texts)


def test_unauthorized_message_names_the_admin(sut, say):
    # The refusal points the user at the admin (first allowlisted ID = U1) so they
    # know who to contact to be added.
    sut._handle_event(_event("UNKNOWN", "hello"), say, MagicMock(), strip_mention=False)
    assert any(f"<@{sut.ADMIN_USER_ID}>" in t for t in say.texts)


def test_admin_is_first_allowlisted_user(sut):
    assert sut.ADMIN_USER_ID == "U1"


def test_group_dm_all_allowlisted_proceeds(sut, say, no_op_agent):
    client = _mpim_client(members=["BOT123", "U1", "U2"])
    sut._handle_event(_group_event("U1", "hi team"), say, client, strip_mention=True)
    assert "reply!" in say.texts


def test_group_dm_with_outsider_is_blocked(sut, say, no_op_agent):
    # U1 is allowlisted and mentions Aspen, but U7 (not allowlisted) is in the room.
    client = _mpim_client(members=["BOT123", "U1", "U7"])
    sut._handle_event(_group_event("U1", "hi team"), say, client, strip_mention=True)
    joined = " ".join(say.texts)
    assert "reply!" not in say.texts          # turn did not run
    assert "approved-users" in joined          # gate message
    assert "name-U7" in joined                 # the outsider is named
    assert f"<@{sut.ADMIN_USER_ID}>" in joined  # admin is tagged
    # Gate runs before rate limiting, so no slot was consumed.
    assert "U1" not in sut._rate_data


def test_group_dm_ignores_bot_members(sut, say, no_op_agent):
    # A non-allowlisted *app* in the group must not trip the human gate.
    client = _mpim_client(members=["BOT123", "U1", "OTHERAPP"], bots=("OTHERAPP",))
    sut._handle_event(_group_event("U1", "hi team"), say, client, strip_mention=True)
    assert "reply!" in say.texts


def test_group_dm_fails_closed_when_membership_unverifiable(sut, say, no_op_agent):
    client = _mpim_client(members=["BOT123", "U1"])
    client.conversations_members.side_effect = Exception("missing scope")
    sut._handle_event(_group_event("U1", "hi team"), say, client, strip_mention=True)
    joined = " ".join(say.texts)
    assert "reply!" not in say.texts
    assert "staying out" in joined


def test_group_dm_detected_via_conversations_info(sut, say, no_op_agent):
    # app_mention events omit channel_type, so the gate must classify via
    # conversations.info and still block an outsider.
    client = _mpim_client(members=["BOT123", "U1", "U7"])
    sut._handle_event(_group_event("U1", "hi team", with_type=False), say, client, strip_mention=True)
    joined = " ".join(say.texts)
    assert "reply!" not in say.texts
    assert "name-U7" in joined


def test_happy_path_strips_mention_and_replies(sut, say, no_op_agent):
    client = MagicMock()
    sut._handle_event(_event("U1", "<@BOT123> hello there"), say, client, strip_mention=True)
    # The native "Aspen is typing…" status replaces the old "_Thinking…_" post.
    client.assistant_threads_setStatus.assert_any_call(
        channel_id="C", thread_ts="1.0", status=sut._STATUS_TEXT
    )
    assert "_Thinking…_" not in say.texts
    assert "reply!" in say.texts
    # The in-flight flag is released in the finally block.
    assert sut._user_active["U1"] is False


def test_status_falls_back_to_thinking_when_setstatus_unavailable(sut, say, no_op_agent):
    # Channel @-mentions aren't assistant threads, so setStatus errors there; the
    # handler must degrade to the plain "_Thinking…_" message rather than fail.
    client = MagicMock()
    client.assistant_threads_setStatus.side_effect = Exception("not an assistant thread")
    sut._handle_event(_event("U1", "<@BOT123> hello"), say, client, strip_mention=True)
    assert "_Thinking…_" in say.texts
    assert "reply!" in say.texts


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
