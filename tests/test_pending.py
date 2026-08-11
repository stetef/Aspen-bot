"""
Tests for the admin request queue.

Aspen deliberately cannot grant access or set a calculations root. What it *can*
do is record that someone asked and hand the admin the exact command — so the
properties here are that nothing is ever granted, that repeat asks don't become
repeat pings, and that a failed DM never breaks the turn the user is waiting on.
"""

from unittest.mock import MagicMock

import pytest


@pytest.fixture
def rq(sut, env):
    env.register(
        {"slack_user_id": "U0SAM", "alias": "sam", "display_name": "Sam", "role": "admin"},
        {"slack_user_id": "U01ARUN", "alias": "arun", "display_name": "Arun N."},
    )
    return sut.pending


def _client():
    client = MagicMock()
    client.conversations_open.return_value = {"channel": {"id": "D0ADMIN"}}
    return client


# --------------------------------------------------------------------------- #
# Recording
# --------------------------------------------------------------------------- #
def test_a_request_is_recorded(sut, rq):
    rq.record("access", "U0NEW", display_name="New Person")
    entry = rq.find("access", "U0NEW")
    assert entry["display_name"] == "New Person" and entry["count"] == 1


def test_repeat_asks_bump_a_counter_instead_of_piling_up(sut, rq):
    for _ in range(4):
        rq.record("access", "U0NEW")
    assert len(rq.load()) == 1
    assert rq.find("access", "U0NEW")["count"] == 4


def test_recording_grants_nothing(sut, rq):
    """The whole point: asking is not getting."""
    rq.record("access", "U0NEW")
    assert sut.registry.by_id("U0NEW") is None
    assert "U0NEW" not in sut.ALLOWED_USER_IDS


def test_a_root_request_carries_the_path(sut, rq):
    rq.record("calc_root", "U01ARUN", detail="/data/arun")
    assert rq.find("calc_root", "U01ARUN")["detail"] == "/data/arun"


def test_an_unknown_kind_is_refused(sut, rq):
    with pytest.raises(ValueError):
        rq.record("sudo", "U01ARUN")


def test_a_corrupt_queue_is_not_fatal(sut, rq):
    sut.REQUESTS_FILE.write_text("{not json")
    assert rq.load() == []          # a lost queue costs a reminder, nothing more


# --------------------------------------------------------------------------- #
# The command the admin runs
# --------------------------------------------------------------------------- #
def test_the_access_command_is_copy_pasteable(sut, rq):
    entry = rq.record("access", "U0NEW", display_name="New Person")
    cmd = rq.command_for(entry)
    assert cmd.startswith("./aspen-users add U0NEW")
    assert "--alias new-person" in cmd and '--name "New Person"' in cmd


def test_the_root_command_names_the_user_by_alias(sut, rq):
    entry = rq.record("calc_root", "U01ARUN", detail="/data/arun")
    assert rq.command_for(entry) == "./aspen-users set-root arun /data/arun"


def test_a_root_request_without_a_path_says_so(sut, rq):
    entry = rq.record("calc_root", "U01ARUN")
    assert "<path-to-their-calculations>" in rq.command_for(entry)


# --------------------------------------------------------------------------- #
# Notifying
# --------------------------------------------------------------------------- #
def test_the_admin_is_dmed_with_the_command(sut, rq, monkeypatch):
    monkeypatch.setattr(sut, "ADMIN_OVERRIDE", "U0SAM")
    client = _client()
    assert rq.notify_admin(client, rq.record("access", "U0NEW", display_name="New")) is True
    text = client.chat_postMessage.call_args.kwargs["text"]
    assert "./aspen-users add U0NEW" in text
    assert client.chat_postMessage.call_args.kwargs["channel"] == "D0ADMIN"


def test_posting_falls_back_to_the_user_id_without_im_write(sut, rq, monkeypatch):
    """conversations.open needs a scope the app may not have been granted."""
    monkeypatch.setattr(sut, "ADMIN_OVERRIDE", "U0SAM")
    client = _client()
    client.conversations_open.side_effect = Exception("missing_scope")
    assert rq.notify_admin(client, rq.record("access", "U0NEW")) is True
    assert client.chat_postMessage.call_args.kwargs["channel"] == "U0SAM"


def test_a_second_ask_does_not_re_ping_within_the_cooldown(sut, rq, monkeypatch):
    monkeypatch.setattr(sut, "ADMIN_OVERRIDE", "U0SAM")
    client = _client()
    rq.notify_admin(client, rq.record("access", "U0NEW"))
    assert rq.notify_admin(client, rq.record("access", "U0NEW")) is False
    assert client.chat_postMessage.call_count == 1


def test_the_cooldown_expires(sut, rq, monkeypatch):
    monkeypatch.setattr(sut, "ADMIN_OVERRIDE", "U0SAM")
    monkeypatch.setattr(sut, "REQUEST_NOTIFY_COOLDOWN_HOURS", 0)
    client = _client()
    rq.notify_admin(client, rq.record("access", "U0NEW"))
    assert rq.notify_admin(client, rq.find("access", "U0NEW")) is True
    assert client.chat_postMessage.call_count == 2


def test_a_failed_dm_is_reported_not_raised(sut, rq, monkeypatch):
    """A Slack outage must not break the refusal the user is waiting on."""
    monkeypatch.setattr(sut, "ADMIN_OVERRIDE", "U0SAM")
    client = _client()
    client.chat_postMessage.side_effect = Exception("slack is down")
    assert rq.notify_admin(client, rq.record("access", "U0NEW")) is False
    assert rq.find("access", "U0NEW") is not None      # still queued for the CLI


def test_no_admin_means_no_dm_but_still_a_queue(sut, rq, monkeypatch):
    """An empty registry is the only way to have nobody to tell — `admin_id`
    falls back to the first active user when no role says admin."""
    monkeypatch.setattr(sut, "ADMIN_OVERRIDE", "")
    monkeypatch.setattr(sut, "BOOTSTRAP_USER_IDS", [])
    sut.registry.save([])
    assert sut.ADMIN_USER_ID == ""
    assert rq.notify_admin(_client(), rq.record("access", "U0NEW")) is False
    assert rq.find("access", "U0NEW") is not None


# --------------------------------------------------------------------------- #
# Resolving
# --------------------------------------------------------------------------- #
def test_granting_access_drops_the_request(sut, rq):
    rq.record("access", "U0NEW", display_name="New")
    sut.registry.save(sut.registry.users() + [
        {"slack_user_id": "U0NEW", "alias": "new", "display_name": "New", "status": "active"}])
    assert len(rq.resolve_satisfied()) == 1
    assert rq.load() == []


def test_setting_a_root_drops_the_request(sut, rq, tmp_path):
    rq.record("calc_root", "U01ARUN", detail=str(tmp_path))
    sut.registry.save([
        dict(u, calc_root=str(tmp_path)) if u["slack_user_id"] == "U01ARUN" else u
        for u in sut.registry.users()])
    assert len(rq.resolve_satisfied()) == 1


def test_an_unsatisfied_request_survives(sut, rq):
    rq.record("access", "U0NEW")
    assert rq.resolve_satisfied() == []
    assert len(rq.load()) == 1


# --------------------------------------------------------------------------- #
# The admission gate raises one by itself
# --------------------------------------------------------------------------- #
def test_being_turned_away_files_a_request(sut, rq, say, monkeypatch):
    monkeypatch.setattr(sut, "ADMIN_OVERRIDE", "U0SAM")
    client = _client()
    client.users_info.return_value = {"user": {"profile": {"display_name": "Newcomer"}}}
    sut._handle_event({"user": "U0NEW", "text": "hi", "ts": "1.0", "channel": "C1"},
                      say, client, strip_mention=False)
    assert "not authorized" in say.texts[0]
    entry = rq.find("access", "U0NEW")
    assert entry["display_name"] == "Newcomer"
    assert "./aspen-users add U0NEW" in client.chat_postMessage.call_args.kwargs["text"]


def test_the_refusal_only_promises_what_happened(sut, rq, say, monkeypatch):
    """If the DM failed, don't tell someone an admin was told."""
    monkeypatch.setattr(sut, "ADMIN_OVERRIDE", "U0SAM")
    client = _client()
    client.chat_postMessage.side_effect = Exception("down")
    sut._handle_event({"user": "U0NEW", "text": "hi", "ts": "1.0", "channel": "C1"},
                      say, client, strip_mention=False)
    assert "send your Slack member ID" in say.texts[0]
    assert "I've let" not in say.texts[0]


def test_an_allowlisted_user_never_reaches_the_request_path(sut, rq, say, monkeypatch):
    """Requests are raised only inside the not-authorized branch.

    Note what this does NOT do: monkeypatch ALLOWED_USER_IDS. That name resolves
    through config's PEP 562 hook, so monkeypatch snapshots the *computed* set and
    restores it as a real attribute — permanently shadowing the hook with whatever
    registry happened to be patched in at the time. Here that would freeze the
    allowlist to this fixture's two users for every test that ran afterwards.
    """
    def _boom(*_a, **_k):
        raise AssertionError("an allowlisted user must never file an access request")

    monkeypatch.setattr(sut.pending, "record", _boom)
    # Arun is on this fixture's allowlist, so the gate lets him past and the
    # refusal branch — the only caller of pending.record — is never taken.
    assert "U01ARUN" in sut.ALLOWED_USER_IDS
    assert sut._request_access("U0NEW", MagicMock()) is False   # the branch itself still guards
