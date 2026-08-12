"""
Integration tests for DEMO mode.

The demo is the one path a *stranger* can reach, so the tests that matter most
are the negative ones. Three properties are load-bearing, and each is asserted
against the real tool surface rather than against ``demo.py`` in isolation:

* **Scope isolation.** A visitor can read the demo tree and nothing else. Not by
  name, not by traversal, not by a cross-root sweep, and not by asking nicely.
* **Nothing is written.** No registry entry (not even a temporary one), no
  workflow file, no metadata sidecar, no request in the admin's queue.
* **Nobody is paged.** The admin request is rendered into the thread; no Slack
  message goes anywhere.

The walkthrough itself is then driven turn by turn over the demo calculations,
which is also the only place the suite exercises the read tools against
realistic ORCA-shaped output.
"""

from unittest.mock import MagicMock

import pytest


@pytest.fixture
def visitor(sut, env, monkeypatch):
    """A stranger — not in the registry, not on the allowlist."""
    env.register(
        {"slack_user_id": "U0SAM", "alias": "sam", "display_name": "Sam",
         "role": "admin", "calc_root": str(env.root("sam-calcs", ["secret-project"]))},
    )
    (env.calcs / "sam-calcs" / "secret-project" / "private.out").write_text(
        "CONFIDENTIAL: real research\n")
    monkeypatch.setattr(sut, "DEMO_ENABLED", True)
    sut.demo.clear()
    yield {"uid": "U0STRANGER", "thread": "1700000000.000100", "env": env}
    sut.demo.clear()


def _event(uid, text, ts="1700000000.000100"):
    return {"user": uid, "text": text, "ts": ts, "channel": "D0DM",
            "channel_type": "im"}


def _ctx(session):
    return {"user_id": session.user_id, "username": "", "thread_ts": session.thread,
            "attachments": [], "slack_client": MagicMock(), "first_turn": False,
            "demo": True}


@pytest.fixture
def session(sut, visitor):
    made, refusal = sut.demo.start(visitor["uid"], visitor["thread"])
    assert made is not None, refusal
    return made


# --------------------------------------------------------------------------- #
# Starting
# --------------------------------------------------------------------------- #
def test_a_stranger_is_normally_refused(sut, visitor, say):
    sut._handle_event(_event(visitor["uid"], "hello"), say, MagicMock(),
                      strip_mention=False)
    assert "not authorized" in say.texts[0]


def test_the_word_demo_starts_a_walkthrough(sut, visitor, say, monkeypatch):
    """The gate is bypassed for a demo, and only for a demo."""
    monkeypatch.setattr(sut.MANAGER, "handle", MagicMock())
    event = _event(visitor["uid"], "DEMO")
    sut._handle_event(event, say, MagicMock(), strip_mention=False)
    # Sessions are keyed the same way agent sessions are (channel + thread).
    assert sut.demo.get(sut._thread_key(event)) is not None
    assert sut.demo.active_for(visitor["uid"]) is not None
    assert not any("not authorized" in (t or "") for t in say.texts)


@pytest.mark.parametrize("text", ["DEMO", "demo", " Demo ", "*demo*", "demo."])
def test_the_trigger_is_forgiving_about_formatting(sut, text):
    assert sut.demo.is_trigger(text)


@pytest.mark.parametrize("text", [
    "can you show me a demo of the thermolysin runs?",
    "demonstrate this",
    "I'd like a demo please",
    "",
])
def test_a_passing_mention_does_not_hijack_a_question(sut, text):
    """Someone asking *about* a demo is asking a question, not starting one."""
    assert not sut.demo.is_trigger(text)


def test_the_demo_can_be_switched_off(sut, visitor, monkeypatch):
    monkeypatch.setattr(sut, "DEMO_ENABLED", False)
    made, refusal = sut.demo.start("U0X", "t1")
    assert made is None and "isn't available" in refusal


def test_there_is_a_daily_ceiling(sut, visitor, monkeypatch):
    monkeypatch.setattr(sut, "DEMO_MAX_STARTS_PER_DAY", 2)
    assert sut.demo.start("U0A", "t1")[0] is not None
    assert sut.demo.start("U0B", "t2")[0] is not None
    made, refusal = sut.demo.start("U0C", "t3")
    assert made is None and "limit" in refusal


def test_a_session_is_capped_in_length(sut, visitor, session, monkeypatch):
    monkeypatch.setattr(sut, "DEMO_MAX_TURNS", 2)
    assert sut.demo.note_turn(session) == ""
    assert sut.demo.note_turn(session) == ""
    assert "end of the demo" in sut.demo.note_turn(session)
    assert sut.demo.get(session.thread) is None      # and it is over


def test_old_sessions_expire(sut, visitor, session, monkeypatch):
    monkeypatch.setattr(sut, "DEMO_SESSION_TTL", 0)
    assert sut.demo.get(session.thread) is None


# --------------------------------------------------------------------------- #
# Scope isolation — the property that makes this safe to expose
# --------------------------------------------------------------------------- #
def test_a_visitor_sees_only_the_demo_projects(sut, session):
    out = sut.dispatch("list_directory", {"path": "."}, _ctx(session))
    assert "fe-porphyrin-scan" in out and "spin-states" in out
    assert "secret-project" not in out


def test_a_visitor_cannot_name_a_real_root(sut, session):
    out = sut.dispatch("list_directory", {"path": ".", "owner": "sam"}, _ctx(session))
    assert "only calculations available are the demo ones" in out
    assert "secret-project" not in out


def test_a_visitor_cannot_reach_a_real_root_by_at_path(sut, session):
    out = sut.dispatch("read_file", {"path": "@sam/secret-project/private.out"},
                       _ctx(session))
    assert "CONFIDENTIAL" not in out
    assert "demo" in out.lower()


def test_a_visitor_cannot_traverse_out(sut, session):
    out = sut.dispatch("read_file", {"path": "../../sam-calcs/secret-project/private.out"},
                       _ctx(session))
    assert "CONFIDENTIAL" not in out
    assert "outside the allowed directory" in out


def test_a_cross_root_sweep_stays_inside_the_demo(sut, session):
    """`everyone=true` is a scope widener for members; for a visitor it must not be."""
    out = sut.dispatch("search_files",
                       {"query": "CONFIDENTIAL", "everyone": True}, _ctx(session))
    assert "CONFIDENTIAL: real research" not in out
    assert "private.out" not in out


def test_the_preamble_does_not_name_real_users(sut, session):
    lines = "\n".join(sut.roots.preamble_lines(session.user_id))
    assert "@sam" not in lines
    assert "demo" in lines


def test_real_users_are_unaffected_by_a_live_demo(sut, session, visitor):
    """A demo running in one thread must not narrow anyone else's view."""
    out = sut.dispatch("list_directory", {"path": "."},
                       {"user_id": "U0SAM", "attachments": []})
    assert "secret-project" in out


# --------------------------------------------------------------------------- #
# Nothing is written
# --------------------------------------------------------------------------- #
def test_a_visitor_never_enters_the_registry(sut, session):
    assert sut.registry.by_id(session.user_id) is None
    assert session.user_id not in sut.ALLOWED_USER_IDS
    assert [u["alias"] for u in sut.registry.users()] == ["sam"]


def test_a_demo_workflow_lives_in_memory_only(sut, session):
    out = sut.dispatch("write_workflow", {"content": "I use BP86/def2-TZVP."},
                       _ctx(session))
    assert "memory only" in out
    assert sut.dispatch("read_workflow", {}, _ctx(session)).count("BP86") == 1
    assert not any(sut.WORKFLOWS_ROOT.glob("*"))          # nothing on disk


def test_demo_notes_live_in_memory_only(sut, session):
    out = sut.dispatch("write_metadata",
                       {"project": "fe-porphyrin-scan", "content": "minimum near 1.95"},
                       _ctx(session))
    assert "in memory for the demo" in out
    assert "1.95" in sut.dispatch("read_metadata", {"project": "fe-porphyrin-scan"},
                                  _ctx(session))
    assert not sut.METADATA_ROOT.exists() or not any(sut.METADATA_ROOT.glob("*"))


def test_demo_notes_cannot_be_written_for_a_project_that_is_not_there(sut, session):
    out = sut.dispatch("write_metadata", {"project": "ghost", "content": "x"},
                       _ctx(session))
    assert "does not exist" in out


def test_a_demo_files_nothing_in_the_admin_queue(sut, session):
    sut.dispatch("demo_request_card", {"what": "access"}, _ctx(session))
    assert sut.pending.load() == []


def test_the_session_is_gone_when_it_ends(sut, session):
    sut.dispatch("write_workflow", {"content": "temporary"}, _ctx(session))
    sut.demo.end(session.thread)
    assert sut.demo.get(session.thread) is None
    assert sut.demo.active_for(session.user_id) is None


# --------------------------------------------------------------------------- #
# Nobody is paged
# --------------------------------------------------------------------------- #
def test_the_request_card_reaches_the_visitor(sut, session):
    """The card must be POSTED, not returned.

    A tool result is only ever seen by the model. Returning the card left the
    agent saying "here's what that looks like:" about something the visitor was
    never shown — which is what happened the first time this ran for real.
    """
    posted = []
    ctx = dict(_ctx(session), on_interim=posted.append)
    out = sut.dispatch("demo_request_card", {"what": "access"}, ctx)

    assert len(posted) == 1
    assert "./aspen-users add" in posted[0]
    assert "not* actually sent" in posted[0] or "not actually sent" in posted[0].replace("*", "")
    # And the model is told it is already visible, so it doesn't paste it twice.
    assert "posted to the thread" in out
    assert "./aspen-users add" not in out


def test_without_an_interim_channel_the_model_is_told_to_paste_it(sut, session):
    """With ASPEN_INTERIM_UPDATES=false the reply is the only thing that reaches
    the visitor, so the card has to come back with instructions."""
    out = sut.dispatch("demo_request_card", {"what": "access"}, _ctx(session))
    assert "VERBATIM" in out
    assert "./aspen-users add" in out


def test_a_failed_post_falls_back_rather_than_vanishing(sut, session):
    def _boom(_text):
        raise RuntimeError("slack is down")

    out = sut.dispatch("demo_request_card", {"what": "access"},
                       dict(_ctx(session), on_interim=_boom))
    assert "VERBATIM" in out and "./aspen-users add" in out


def test_the_card_is_never_sent_to_anyone(sut, session):
    ctx = dict(_ctx(session), on_interim=lambda _t: None)
    sut.dispatch("demo_request_card", {"what": "access"}, ctx)
    ctx["slack_client"].chat_postMessage.assert_not_called()
    ctx["slack_client"].conversations_open.assert_not_called()


def test_the_root_request_card_shows_the_set_root_command(sut, session):
    out = sut.dispatch("demo_request_card",
                       {"what": "calc_root", "path": "/data/mine"}, _ctx(session))
    assert "set-root" in out and "/data/mine" in out


def test_approving_only_moves_the_walkthrough_on(sut, session):
    out = sut.dispatch("demo_approve", {}, _ctx(session))
    assert session.approved is True
    assert "an admin ran that command" in out
    assert sut.registry.by_id(session.user_id) is None   # still nobody


def test_the_demo_tools_do_nothing_outside_a_demo(sut, visitor):
    ctx = {"user_id": "U0SAM", "attachments": []}
    assert "only available inside a demo" in sut.dispatch("demo_approve", {}, ctx)
    assert "only available inside a demo" in sut.dispatch("demo_request_card", {}, ctx)


# --------------------------------------------------------------------------- #
# The walkthrough over realistic output
# --------------------------------------------------------------------------- #
def test_the_scan_reads_like_orca_output(sut, session):
    out = sut.dispatch("read_file",
                       {"path": "fe-porphyrin-scan/d1.95/feo-1p95-orca.log"}, _ctx(session))
    assert "FINAL SINGLE POINT ENERGY" in out
    assert "SCF CONVERGED" in out
    assert "MULLIKEN ATOMIC CHARGES" in out


def test_the_failed_run_is_findable(sut, session):
    """The demo beat that shows Aspen answering 'which of my runs failed?'."""
    out = sut.dispatch("search_files", {"query": "NOT CONVERGED"}, _ctx(session))
    assert "d2.30" in out
    assert out.count("orca.log") == 1        # exactly one run failed


def test_every_converged_run_reports_an_energy(sut, session):
    out = sut.dispatch("search_files",
                       {"query": "FINAL SINGLE POINT ENERGY"}, _ctx(session))
    assert out.count("FINAL SINGLE POINT ENERGY") >= 7   # 5 scan + 2 spin states


def test_the_energies_form_a_curve_with_a_minimum(sut, session):
    """What the demo's plot is supposed to show, asserted on the data itself."""
    import re
    energies = {}
    for distance in ("1.80", "1.90", "1.95", "2.00", "2.10"):
        name = f"feo-{distance.replace('.', 'p')}-orca.log"
        text = sut.dispatch(
            "read_file", {"path": f"fe-porphyrin-scan/d{distance}/{name}"}, _ctx(session))
        energies[float(distance)] = float(
            re.search(r"FINAL SINGLE POINT ENERGY\s+(-?\d+\.\d+)", text).group(1))
    assert min(energies, key=energies.get) == 1.95
    assert energies[1.80] > energies[1.90] > energies[1.95] < energies[2.00] < energies[2.10]


def test_the_spin_states_can_be_compared(sut, session):
    out = sut.dispatch("list_directory", {"path": "spin-states"}, _ctx(session))
    assert "high-spin" in out and "low-spin" in out


def test_the_guidance_walks_through_the_stages(sut, session):
    first = "\n".join(sut.demo.guidance_lines(session))
    assert "demo_request_card" in first
    session.advance("approved")
    assert "workflow" in "\n".join(sut.demo.guidance_lines(session))
    session.advance("analyze")
    assert "run_python_analysis" in "\n".join(sut.demo.guidance_lines(session))


def test_the_guidance_says_the_data_is_fabricated(sut, session):
    assert "fabricated" in "\n".join(sut.demo.guidance_lines(session))
