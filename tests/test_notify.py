"""
Tests for job notifications (``aspen/notify.py``).

Aspen's silence used to carry no information — a batch could be finished, failed, or
queued and the only way to tell was to ask. The behaviours worth pinning:

* **A failure interrupts; success waits.** A dependency chain fails *late*, so
  waiting for every job to settle would keep a run that died in its first minute
  quiet for hours.
* **One notification per batch**, not one per job.
* **Sent once**, whether or not delivery worked — a channel the bot was removed from
  must not become an infinite retry.
* **The preference is remembered**, so nobody is asked twice, and a saved answer
  beats whatever the current call passes.
"""

import pytest


@pytest.fixture
def batch(sut, env):
    env.register(
        {"slack_user_id": "U0SAM", "alias": "sam", "display_name": "Sam", "role": "admin"},
        {"slack_user_id": "U01ARUN", "alias": "arun", "display_name": "Arun N."},
    )

    def make(uid="U0SAM", notify=True, channel="C123", thread="1723480000.1"):
        return sut.jobs.record_batch(
            slack_user_id=uid, alias="sam", thread_ts=thread, project="thermolysin",
            owner_scope="sam", template_mode="interp",
            staging_dir=sut.JOBS_STAGING_ROOT / "x", structures=1,
            argv=["xas-run-batch"], channel=channel, notify=notify,
        )
    return make


class Recorder:
    """Stand-in for the Slack client."""

    def __init__(self, fail_channels=()):
        self.posts = []
        self._fail = set(fail_channels)

    def chat_postMessage(self, channel, text, thread_ts=None):
        if channel in self._fail:
            raise RuntimeError("channel_not_found")
        self.posts.append({"channel": channel, "text": text, "thread_ts": thread_ts})
        return {"ok": True}


def _settle(sut, batch_id, *states):
    """Record jobs for a batch and give them terminal states."""
    entries = [{"job_id": str(9000 + i), "kind": k, "work_dir": "/x"}
               for i, k in enumerate(("orca", "corvus", "postprocess"))]
    sut.jobs.record_jobs(batch_id, entries)
    sut.jobs.apply_reconciliation(
        [{"job_id": e["job_id"], "state": s} for e, s in zip(entries, states)])


@pytest.fixture(autouse=True)
def _no_sacct(sut, monkeypatch):
    """The poller refreshes first; don't let tests shell out to a real cluster.

    Both seams, deliberately: ``refresh_states`` is what ``due()`` calls, and
    ``reconcile_quietly`` is what it would reach for underneath. On a login node
    the real squeue exists, so patching only the inner one would leave the suite
    querying the actual queue.
    """
    monkeypatch.setattr(sut.jobs, "refresh_states", lambda days=30: False)
    monkeypatch.setattr(sut.jobs, "reconcile_quietly", lambda days=30: None)


# --------------------------------------------------------------------------- #
# When something is due
# --------------------------------------------------------------------------- #
def test_a_finished_batch_is_due(sut, batch):
    bid = batch()
    _settle(sut, bid, "COMPLETED", "COMPLETED", "COMPLETED")
    items = sut.notify.due()
    assert [i["batch_id"] for i in items] == [bid]
    assert items[0]["reason"] == "finished"
    assert "finished" in items[0]["text"]


def test_a_failure_notifies_before_the_rest_have_settled(sut, batch):
    """The point of notifying at all: a chain fails late, so waiting is wrong."""
    bid = batch()
    _settle(sut, bid, "FAILED", "PENDING", "PENDING")
    items = sut.notify.due()
    assert len(items) == 1 and items[0]["reason"] == "failed"
    assert "9000" in items[0]["text"], "the message should name what failed"


def test_a_running_batch_is_not_due(sut, batch):
    bid = batch()
    _settle(sut, bid, "RUNNING", "PENDING", "PENDING")
    assert sut.notify.due() == []


def test_a_batch_with_no_recorded_jobs_is_not_due(sut, batch):
    """Backfill may still fix it; notifying "finished" would be a lie."""
    batch()
    assert sut.notify.due() == []


def test_opting_out_means_never_due(sut, batch):
    bid = batch(notify=False)
    _settle(sut, bid, "COMPLETED", "COMPLETED", "COMPLETED")
    assert sut.notify.due() == []


def test_one_notification_per_batch_not_per_job(sut, batch):
    bid = batch()
    _settle(sut, bid, "FAILED", "FAILED", "CANCELLED")
    assert len(sut.notify.due()) == 1


def test_a_batch_is_not_notified_twice(sut, batch):
    bid = batch()
    _settle(sut, bid, "COMPLETED", "COMPLETED", "COMPLETED")
    client = Recorder()
    assert sut.notify.run_once(client) == 1
    assert sut.notify.run_once(client) == 0, "already told them"
    assert len(client.posts) == 1


# --------------------------------------------------------------------------- #
# Delivery
# --------------------------------------------------------------------------- #
def test_it_posts_into_the_originating_thread(sut, batch):
    bid = batch(channel="C123", thread="1723480000.1")
    _settle(sut, bid, "COMPLETED", "COMPLETED", "COMPLETED")
    client = Recorder()
    sut.notify.run_once(client)
    assert client.posts[0]["channel"] == "C123"
    assert client.posts[0]["thread_ts"] == "1723480000.1", (
        "the thread holds the context — the diff they approved, the structures chosen"
    )


def test_it_falls_back_to_a_dm_when_the_thread_is_unreachable(sut, batch):
    bid = batch(channel="C-GONE")
    _settle(sut, bid, "COMPLETED", "COMPLETED", "COMPLETED")
    client = Recorder(fail_channels={"C-GONE"})
    assert sut.notify.run_once(client) == 1
    assert client.posts[0]["channel"] == "U0SAM", "DM the user rather than drop it"


def test_a_batch_with_no_channel_is_dm_ed(sut, batch):
    bid = batch(channel="")
    _settle(sut, bid, "COMPLETED", "COMPLETED", "COMPLETED")
    client = Recorder()
    sut.notify.run_once(client)
    assert client.posts[0]["channel"] == "U0SAM"


def test_undeliverable_is_still_marked_sent(sut, batch):
    """Otherwise a removed channel becomes an unbounded retry loop."""
    bid = batch(channel="C-GONE")
    _settle(sut, bid, "COMPLETED", "COMPLETED", "COMPLETED")

    class Dead(Recorder):
        def chat_postMessage(self, channel, text, thread_ts=None):
            raise RuntimeError("nope")

    assert sut.notify.run_once(Dead()) == 0
    assert sut.notify.due() == [], "one good attempt, not an infinite one"


def test_the_message_says_where_the_results_are(sut, batch):
    bid = batch()
    _settle(sut, bid, "COMPLETED", "COMPLETED", "COMPLETED")
    text = sut.notify.due()[0]["text"]
    assert "Results are in" in text
    assert str(sut.JOBS_STAGING_ROOT) in text


# --------------------------------------------------------------------------- #
# The remembered preference
# --------------------------------------------------------------------------- #
def test_a_preference_is_saved_and_read_back(sut, batch):
    assert sut.notify.preference("U0SAM") == sut.notify.UNSET
    out = sut.notify.set_preference("U0SAM", "always")
    assert "I'll tell you" in out
    assert sut.notify.preference("U0SAM") == sut.notify.ALWAYS
    sut.notify.set_preference("U0SAM", "never")
    assert sut.notify.preference("U0SAM") == sut.notify.NEVER


def test_a_preference_is_per_user(sut, batch):
    sut.notify.set_preference("U0SAM", "always")
    assert sut.notify.preference("U01ARUN") == sut.notify.UNSET


def test_a_saved_preference_beats_what_the_call_passes(sut, batch, monkeypatch):
    """So nobody is asked twice, and an answer already given is not overridden."""
    monkeypatch.setattr(sut, "JOBS_NOTIFY_ENABLED", True, raising=False)
    sut.notify.set_preference("U0SAM", "never")
    assert sut.tools._wants_notification("U0SAM", {"notify": True}) is False

    sut.notify.set_preference("U0SAM", "always")
    assert sut.tools._wants_notification("U0SAM", {"notify": False}) is True

    # Unset: the call decides, and does not persist by itself.
    assert sut.tools._wants_notification("U01ARUN", {"notify": True}) is True
    assert sut.notify.preference("U01ARUN") == sut.notify.UNSET


def test_a_bad_choice_is_refused(sut, batch):
    for bad in ("", "maybe", "ALWAYS!", "yes"):
        assert "Error" in sut.notify.set_preference("U0SAM", bad)
    assert sut.notify.preference("U0SAM") == sut.notify.UNSET


def test_an_unregistered_user_cannot_set_one(sut, batch):
    assert "Error" in sut.notify.set_preference("UVISITOR", "always")


def test_the_switch_turns_everything_off(sut, batch, monkeypatch):
    monkeypatch.setattr(sut, "JOBS_NOTIFY_ENABLED", False, raising=False)
    sut.notify.set_preference("U0SAM", "always")
    assert sut.tools._wants_notification("U0SAM", {"notify": True}) is False


def test_the_tool_only_sets_the_speakers_own_preference(sut):
    """Same rule as every other write: no owner parameter."""
    spec = next(s for s in sut.TOOL_SPECS if s["name"] == "set_job_notifications")
    props = set(spec["input_schema"]["properties"])
    assert props == {"choice"}


# --------------------------------------------------------------------------- #
# The watcher must not take the bot down
# --------------------------------------------------------------------------- #
def test_the_loop_watches_often_while_jobs_are_outstanding(sut, monkeypatch):
    """One interval had to trade promptness against polling slurmdbd forever.
    Two intervals do not, because the frequent case is also the cheap one."""
    monkeypatch.setattr(sut, "JOBS_NOTIFY_ACTIVE_POLL_SECONDS", 45, raising=False)
    monkeypatch.setattr(sut, "JOBS_NOTIFY_POLL_SECONDS", 600, raising=False)
    assert sut.notify.interval_for(3) == 45
    assert sut.notify.interval_for(0) == 600


def test_neither_interval_goes_below_the_floor(sut, monkeypatch):
    monkeypatch.setattr(sut, "JOBS_NOTIFY_ACTIVE_POLL_SECONDS", 1, raising=False)
    monkeypatch.setattr(sut, "JOBS_NOTIFY_POLL_SECONDS", 1, raising=False)
    assert sut.notify.interval_for(1) == sut.notify.MIN_INTERVAL
    assert sut.notify.interval_for(0) == sut.notify.MIN_INTERVAL


def test_an_idle_interval_below_the_active_one_is_lifted(sut, monkeypatch):
    """A misconfiguration must not make a quiet bot poll harder than a busy one."""
    monkeypatch.setattr(sut, "JOBS_NOTIFY_ACTIVE_POLL_SECONDS", 120, raising=False)
    monkeypatch.setattr(sut, "JOBS_NOTIFY_POLL_SECONDS", 60, raising=False)
    assert sut.notify.interval_for(0) == 120


def test_the_loop_sleeps_by_what_is_outstanding(sut, batch, monkeypatch):
    import threading

    monkeypatch.setattr(sut, "JOBS_NOTIFY_ACTIVE_POLL_SECONDS", 45, raising=False)
    monkeypatch.setattr(sut, "JOBS_NOTIFY_POLL_SECONDS", 600, raising=False)
    bid = batch()
    _settle(sut, bid, "RUNNING", "PENDING", "PENDING")

    slept, stop = [], threading.Event()

    def record(seconds):
        slept.append(seconds)
        stop.set()

    stop.wait = record
    sut.notify.watcher(Recorder(), stop=stop)
    assert slept == [45], "three jobs are in flight; look again soon"


def test_a_failing_poll_does_not_stop_the_loop(sut, monkeypatch):
    import threading

    calls = []

    def boom(client):
        calls.append(1)
        raise RuntimeError("sacct exploded")

    monkeypatch.setattr(sut.notify, "run_once", boom)
    monkeypatch.setattr(sut, "JOBS_NOTIFY_POLL_SECONDS", 60, raising=False)
    stop = threading.Event()

    def stop_after_two(_seconds):
        if len(calls) >= 2:
            stop.set()

    stop.wait = stop_after_two
    sut.notify.watcher(object(), stop=stop)
    assert len(calls) >= 2, "a bad pass must not end the watcher"
