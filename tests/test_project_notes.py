"""
Tests for encouraging a project description — the offer, and its rationing.

A project needs no notes to be read or analysed (that requirement, and the 422
that enforced it, is gone — see test_tool_server_roots). What replaces it is an
*offer*, made where an undescribed project is noticed and silenced by everything
that should silence it. These tests are mostly about when Aspen keeps quiet,
because a nudge that fires too often is the failure mode this design exists to
avoid.
"""

import pytest


@pytest.fixture(autouse=True)
def _fresh_ration(sut):
    """The once-per-project ration is process state; don't leak it across tests."""
    sut.setup._offered.clear()
    yield
    sut.setup._offered.clear()


@pytest.fixture
def two_users(sut, env):
    """Sam and Arun, each with a root and one undocumented project."""
    sam_root = env.root("sam-calcs", ["thermolysin"])
    arun_root = env.root("arun-calcs", ["dft"])
    (sam_root / "thermolysin" / "run_001").mkdir()
    (sam_root / "thermolysin" / "run_001" / "run_001.log").write_text("converged\n")
    env.register(
        {"slack_user_id": "U0SAM", "alias": "sam", "display_name": "Sam",
         "role": "admin", "calc_root": str(sam_root)},
        {"slack_user_id": "U01ARUN", "alias": "arun", "display_name": "Arun N.",
         "calc_root": str(arun_root)},
    )
    return {"sam": sam_root, "arun": arun_root}


def _ctx(uid):
    return {"user_id": uid, "username": "", "thread_ts": "1.0", "attachments": []}


def _list(sut, uid, path, **kw):
    return sut.dispatch("list_directory", {"path": path, **kw}, _ctx(uid))


# --------------------------------------------------------------------------- #
# The offer
# --------------------------------------------------------------------------- #
def test_an_undescribed_project_gets_an_offer(sut, two_users):
    out = _list(sut, "U0SAM", "thermolysin")
    assert "README" in out
    assert "Python libraries available for analysis" in out    # the shape to follow
    assert "run_001" in out                                    # the listing still came back


def test_the_offer_says_aspen_cannot_write_it(sut, two_users):
    """The one thing the model must not get wrong: it drafts, the user saves."""
    out = _list(sut, "U0SAM", "thermolysin")
    assert "read-only" in out
    assert "thermolysin/README.md" in out


def test_the_offer_names_the_alternative_for_people_who_wont_keep_a_file(sut, two_users):
    out = _list(sut, "U0SAM", "thermolysin")
    assert "write_metadata" in out
    assert "decline_setup" in out


# --------------------------------------------------------------------------- #
# When it stays quiet
# --------------------------------------------------------------------------- #
def test_it_is_offered_only_once_per_project(sut, two_users):
    assert "README" in _list(sut, "U0SAM", "thermolysin")
    assert "README" not in _list(sut, "U0SAM", "thermolysin")


def test_an_existing_readme_silences_it(sut, two_users):
    (two_users["sam"] / "thermolysin" / "README.md").write_text("Zn site models.\n")
    out = _list(sut, "U0SAM", "thermolysin")
    assert "[file] README.md" in out              # the listing shows it, as always
    assert "decline_setup" not in out             # and nothing asks for another


@pytest.mark.parametrize("name", ["README", "readme.md", "README.txt", "metadata.md"])
def test_any_in_tree_description_silences_it(sut, two_users, name):
    (two_users["sam"] / "thermolysin" / name).write_text("Zn site models.\n")
    assert "decline_setup" not in _list(sut, "U0SAM", "thermolysin")


def test_aspens_own_notes_silence_it_too(sut, two_users):
    """Told once through a different door is still told once."""
    sut.dispatch("write_metadata",
                 {"project": "thermolysin", "content": "Zn site models."}, _ctx("U0SAM"))
    out = _list(sut, "U0SAM", "thermolysin")
    assert "decline_setup" not in out
    assert "Aspen has metadata" in out                 # the existing summary line, unchanged


def test_a_colleagues_project_is_not_yours_to_document(sut, two_users):
    assert "README" not in _list(sut, "U0SAM", "dft", owner="arun")


def test_a_shared_root_is_not_offered(sut, env, two_users):
    env.shared("smb", ["csd"])
    assert "README" not in _list(sut, "U0SAM", "csd", owner="smb")


def test_a_run_directory_deeper_in_is_not_the_place(sut, two_users):
    """From inside a run, 'this project has no README' is noise."""
    assert "README" not in _list(sut, "U0SAM", "thermolysin/run_001")


def test_the_root_listing_is_not_the_place_either(sut, two_users):
    assert "README" not in _list(sut, "U0SAM", ".")


def test_declining_silences_it_for_every_project(sut, env, two_users):
    (two_users["sam"] / "porphyrin").mkdir()
    assert "Noted" in sut.dispatch(
        "decline_setup", {"item": "project_notes"}, _ctx("U0SAM"))
    assert "README" not in _list(sut, "U0SAM", "thermolysin")
    assert "README" not in _list(sut, "U0SAM", "porphyrin")


def test_a_declined_user_is_still_offered_the_other_items(sut, two_users):
    """project_notes is its own switch, not a blanket 'stop helping me'."""
    sut.dispatch("decline_setup", {"item": "project_notes"}, _ctx("U0SAM"))
    assert "workflow" in sut.setup.missing("U0SAM")


def test_unknown_items_are_still_refused(sut, two_users):
    assert sut.dispatch(
        "decline_setup", {"item": "everything"}, _ctx("U0SAM")).startswith("Error")


# --------------------------------------------------------------------------- #
# Reading what the owner wrote
# --------------------------------------------------------------------------- #
def test_read_metadata_points_at_the_owners_readme(sut, two_users):
    """Their README answers 'what is this project?' better than Aspen's blank."""
    (two_users["sam"] / "thermolysin" / "README.md").write_text("Zn site models.\n")
    out = sut.dispatch("read_metadata", {"project": "thermolysin"}, _ctx("U0SAM"))
    assert "README.md" in out and "read_file" in out


def test_read_metadata_says_nothing_of_a_readme_that_is_not_there(sut, two_users):
    out = sut.dispatch("read_metadata", {"project": "thermolysin"}, _ctx("U0SAM"))
    assert "README" not in out
    assert "No metadata recorded" in out


def test_the_readme_lookup_cannot_traverse_out(sut, two_users):
    scope = {"path": two_users["sam"], "name": "sam", "owner_id": "U0SAM"}
    assert sut.metadata.in_tree_notes("../arun-calcs/dft", scope) is None
    assert sut.metadata.in_tree_notes("..", scope) is None
