"""
Tests for getting people set up, and for knowing when to stop asking.

Three behaviours carry the weight:

* **Declining is remembered.** Someone who says no is not asked again — the old
  preamble carried the offer in every turn, forever.
* **Declining a root changes resolution, not just volume.** A reader who owns no
  calculations must not be handed the shared default as "your files".
* **Nothing here grants anything.** A root is *requested*; only the CLI sets one.
"""

from unittest.mock import MagicMock

import pytest


@pytest.fixture
def env(sut, tmp_path, monkeypatch):
    # Keep state and calculations in separate subtrees: a root inside STATE_DIR
    # is correctly refused, which would otherwise mask what these tests check.
    state = tmp_path / "state"
    calcs = tmp_path / "calcs"
    state.mkdir(), calcs.mkdir()
    monkeypatch.setattr(sut, "USERS_FILE", state / "users.json")
    monkeypatch.setattr(sut, "REQUESTS_FILE", state / "requests.json")
    monkeypatch.setattr(sut, "STATE_DIR", state)
    root = state / "workflows"
    root.mkdir()
    monkeypatch.setattr(sut, "WORKFLOWS_ROOT", root)
    monkeypatch.setattr(sut, "WORKSPACE_ROOT", tmp_path / "workspace")
    arun_root = calcs / "arun-calcs"
    arun_root.mkdir()

    sut.registry.invalidate()
    sut.registry.save([
        {"slack_user_id": "U0SAM", "alias": "sam", "display_name": "Sam",
         "role": "admin", "status": "active"},
        {"slack_user_id": "U01ARUN", "alias": "arun", "display_name": "Arun N.",
         "role": "member", "status": "active", "calc_root": str(arun_root)},
        {"slack_user_id": "U0PI", "alias": "pi", "display_name": "The PI",
         "role": "member", "status": "active"},
    ])
    yield {"tmp": calcs, "arun_root": arun_root}
    sut.registry.invalidate()


def _ctx(uid):
    return {"user_id": uid, "username": "", "thread_ts": "1.0", "attachments": [],
            "slack_client": MagicMock(), "first_turn": True}


# --------------------------------------------------------------------------- #
# State
# --------------------------------------------------------------------------- #
def test_having_one_is_derived_not_stored(sut, env):
    assert sut.setup.state("U01ARUN", "calc_root") == "has"
    assert sut.setup.state("U0PI", "calc_root") == "missing"


def test_a_workflow_counts_once_it_exists(sut, env):
    assert sut.setup.state("U0SAM", "workflow") == "missing"
    sut.workflows.write("U0SAM", "---\ndescription: d\n---\n\nbody\n")
    assert sut.setup.state("U0SAM", "workflow") == "has"


def test_declining_is_remembered(sut, env):
    sut.setup.decline("U0PI", "workflow")
    assert sut.setup.state("U0PI", "workflow") == "declined"
    assert sut.registry.by_id("U0PI")["declined"]["workflow"]


def test_declining_something_you_have_is_refused(sut, env):
    assert "already have" in sut.setup.decline("U01ARUN", "calc_root")


def test_an_unknown_item_is_refused(sut, env):
    assert "not something to set up" in sut.setup.decline("U0PI", "sudo")


def test_a_decline_can_be_undone(sut, env):
    sut.setup.decline("U0PI", "workflow")
    assert sut.setup.undecline("U0PI", "workflow") is True
    assert sut.setup.state("U0PI", "workflow") == "missing"


# --------------------------------------------------------------------------- #
# Nudging
# --------------------------------------------------------------------------- #
def test_only_one_thing_is_asked_at_a_time(sut, env):
    lines = sut.setup.nudge_lines("U0PI", first_turn=True)
    assert len(lines) == 1


def test_nothing_is_asked_mid_thread(sut, env):
    assert sut.setup.nudge_lines("U0PI", first_turn=False) == []


def test_a_declined_item_is_never_raised_again(sut, env):
    sut.setup.decline("U0PI", "workflow")
    sut.setup.decline("U0PI", "calc_root")
    assert sut.setup.nudge_lines("U0PI", first_turn=True) == []


def test_someone_with_both_is_left_alone(sut, env):
    sut.workflows.write("U01ARUN", "---\ndescription: d\n---\n\nbody\n")
    assert sut.setup.nudge_lines("U01ARUN", first_turn=True) == []


def test_the_root_nudge_explains_it_needs_an_admin(sut, env):
    sut.setup.decline("U0PI", "workflow")     # so calc_root is what comes up
    line = sut.setup.nudge_lines("U0PI", first_turn=True)[0]
    assert "request_calc_root" in line and "you cannot" in line


def test_the_preamble_carries_the_nudge(sut, env):
    out = sut.workflows.turn_preamble(
        "U0PI", extra_lines=sut.setup.nudge_lines("U0PI", first_turn=True))
    assert "write_workflow" in out


# --------------------------------------------------------------------------- #
# Declining a root changes what an unqualified path means
# --------------------------------------------------------------------------- #
def test_a_rootless_user_is_not_handed_the_shared_default(sut, env):
    """The PI's 'my files' must not silently resolve to somebody else's tree."""
    before, _scope, err = sut.roots.resolve(".", "", "U0PI")
    assert not err and before == sut.CALCULATIONS_ROOT      # today's fallback

    sut.setup.decline("U0PI", "calc_root")
    _path, _scope, err = sut.roots.resolve(".", "", "U0PI")
    assert "no calculations of their own" in err
    assert "@" in err                                       # it names the alternative


def test_a_rootless_user_can_still_read_everyone(sut, env):
    sut.setup.decline("U0PI", "calc_root")
    path, scope, err = sut.roots.resolve(".", "arun", "U0PI")
    assert not err and path == env["arun_root"] and scope["name"] == "arun"


def test_the_tools_report_that_clearly(sut, env):
    sut.setup.decline("U0PI", "calc_root")
    out = sut.dispatch("list_directory", {"path": "."}, _ctx("U0PI"))
    assert "no calculations of their own" in out


def test_declining_does_not_affect_anyone_else(sut, env):
    sut.setup.decline("U0PI", "calc_root")
    path, _scope, err = sut.roots.resolve(".", "", "U0SAM")
    assert not err and path == sut.CALCULATIONS_ROOT


# --------------------------------------------------------------------------- #
# Requesting a root
# --------------------------------------------------------------------------- #
def test_requesting_a_root_files_it_and_tells_the_admin(sut, env, monkeypatch):
    monkeypatch.setattr(sut, "ADMIN_OVERRIDE", "U0SAM")
    ctx = _ctx("U0PI")
    new_root = env["tmp"] / "pi-calcs"
    new_root.mkdir()
    out = sut.dispatch("request_calc_root", {"path": str(new_root)}, ctx)
    assert "approve" in out
    entry = sut.pending.find("calc_root", "U0PI")
    assert entry["detail"] == str(new_root)
    assert "set-root pi" in ctx["slack_client"].chat_postMessage.call_args.kwargs["text"]


def test_requesting_grants_nothing(sut, env):
    new_root = env["tmp"] / "pi-calcs2"
    new_root.mkdir()
    sut.dispatch("request_calc_root", {"path": str(new_root)}, _ctx("U0PI"))
    assert sut.roots.for_user("U0PI") == sut.CALCULATIONS_ROOT
    assert not sut.registry.by_id("U0PI")["calc_root"]


def test_an_unusable_path_is_still_filed_but_flagged(sut, env):
    """A path Aspen can't verify is still a person asking — the admin can fix it."""
    out = sut.dispatch("request_calc_root", {"path": "/nonexistent/nope"}, _ctx("U0PI"))
    assert "couldn't verify" in out and "does not exist" in out
    assert sut.pending.find("calc_root", "U0PI") is not None


def test_a_request_without_a_path_still_registers_interest(sut, env):
    out = sut.dispatch("request_calc_root", {}, _ctx("U0PI"))
    assert "let" in out and "know" in out
    assert sut.pending.find("calc_root", "U0PI") is not None


def test_a_request_is_always_for_the_speaker(sut, env):
    """No wording can file a request on someone else's behalf — there is no field."""
    schema = next(s for s in sut.TOOL_SPECS if s["name"] == "request_calc_root")
    assert "owner" not in schema["input_schema"]["properties"]
    assert "user" not in schema["input_schema"]["properties"]


def test_declining_a_root_withdraws_a_pending_request(sut, env):
    sut.dispatch("request_calc_root", {}, _ctx("U0PI"))
    sut.dispatch("decline_setup", {"item": "calc_root"}, _ctx("U0PI"))
    assert sut.pending.find("calc_root", "U0PI") is None


def test_decline_setup_is_for_the_speaker_only(sut, env):
    schema = next(s for s in sut.TOOL_SPECS if s["name"] == "decline_setup")
    assert list(schema["input_schema"]["properties"]) == ["item"]
