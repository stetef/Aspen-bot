"""
Tests for the tool surface once calculations are split by owner.

Reading is flat — everyone may read every root, as on the shared filesystem — so
these mostly pin *whose* files a call reaches, that the answer says whose they
were, and that the one write surface stays out of every calculations tree.
"""

import pytest


@pytest.fixture
def multi(sut, tmp_path, monkeypatch):
    """Sam and Arun with separate roots, a shared root, and some content."""
    monkeypatch.setattr(sut, "USERS_FILE", tmp_path / "users.json")
    sam_root, arun_root, shared_root = (tmp_path / n for n in
                                        ("sam-calcs", "arun-calcs", "group-calcs"))
    for root in (sam_root, arun_root, shared_root):
        root.mkdir()
    (sam_root / "thermolysin").mkdir()
    (sam_root / "thermolysin" / "notes.txt").write_text("sam: zinc site converged\n")
    (arun_root / "dft").mkdir()
    (arun_root / "dft" / "orca.out").write_text("arun: BP86/def2-TZVP converged\n")
    (shared_root / "csd").mkdir()
    (shared_root / "csd" / "index.txt").write_text("shared: CSD entries\n")

    monkeypatch.setattr(sut, "SHARED_CALC_ROOTS", {"smb": str(shared_root)})
    monkeypatch.setattr(sut, "METADATA_ROOT", tmp_path / "metadata")
    monkeypatch.setattr(sut, "METADATA_HISTORY_ROOT", tmp_path / "metadata_history")

    sut.registry.invalidate()
    sut.registry.save([
        {"slack_user_id": "U0SAM", "alias": "sam", "display_name": "Sam",
         "role": "admin", "status": "active", "calc_root": str(sam_root)},
        {"slack_user_id": "U01ARUN", "alias": "arun", "display_name": "Arun N.",
         "role": "member", "status": "active", "calc_root": str(arun_root)},
    ])
    yield {"sam": sam_root, "arun": arun_root, "shared": shared_root}
    sut.registry.invalidate()


def _ctx(uid):
    return {"user_id": uid, "username": "", "thread_ts": "1.0", "attachments": []}


# --------------------------------------------------------------------------- #
# Reading across roots
# --------------------------------------------------------------------------- #
def test_a_bare_listing_shows_your_own_projects(sut, multi):
    out = sut.dispatch("list_directory", {"path": "."}, _ctx("U0SAM"))
    assert "thermolysin" in out and "dft" not in out


def test_owner_reaches_a_colleagues_files(sut, multi):
    out = sut.dispatch("read_file", {"path": "dft/orca.out", "owner": "arun"}, _ctx("U0SAM"))
    assert "BP86/def2-TZVP" in out


def test_an_at_path_reaches_them_too(sut, multi):
    out = sut.dispatch("read_file", {"path": "@arun/dft/orca.out"}, _ctx("U0SAM"))
    assert "BP86/def2-TZVP" in out


def test_the_same_relative_path_means_different_files_per_speaker(sut, multi):
    """The heart of it: 'dft/orca.out' is Arun's file only when Arun asks."""
    sam = sut.dispatch("list_directory", {"path": "."}, _ctx("U0SAM"))
    arun = sut.dispatch("list_directory", {"path": "."}, _ctx("U01ARUN"))
    assert "thermolysin" in sam and "thermolysin" not in arun
    assert "dft" in arun and "dft" not in sam


def test_shared_roots_are_reachable_by_name(sut, multi):
    out = sut.dispatch("read_file", {"path": "csd/index.txt", "owner": "smb"}, _ctx("U01ARUN"))
    assert "CSD entries" in out


def test_reading_out_of_a_root_is_refused(sut, multi):
    out = sut.dispatch("read_file", {"path": "../arun-calcs/dft/orca.out"}, _ctx("U0SAM"))
    assert "outside the allowed directory" in out


def test_attach_file_honours_the_owner(sut, multi):
    ctx = _ctx("U0SAM")
    sut.dispatch("attach_file", {"path": "dft/orca.out", "owner": "arun"}, ctx)
    assert ctx["attachments"] == [str(multi["arun"] / "dft" / "orca.out")]


# --------------------------------------------------------------------------- #
# Search
# --------------------------------------------------------------------------- #
def test_search_defaults_to_your_own_files(sut, multi):
    out = sut.dispatch("search_files", {"query": "converged"}, _ctx("U0SAM"))
    assert "zinc site" in out and "BP86" not in out


def test_search_everyone_sweeps_all_roots_and_says_whose(sut, multi):
    out = sut.dispatch("search_files", {"query": "converged", "everyone": True}, _ctx("U0SAM"))
    assert "@sam/thermolysin/notes.txt" in out
    assert "@arun/dft/orca.out" in out


def test_search_everyone_covers_shared_roots(sut, multi):
    out = sut.dispatch("search_files", {"query": "CSD", "everyone": True}, _ctx("U0SAM"))
    assert "@smb/csd/index.txt" in out


def test_a_truncated_sweep_never_reads_as_a_complete_one(sut, multi, monkeypatch):
    monkeypatch.setattr(sut, "SEARCH_MAX_FILES_ALL", 1)
    out = sut.dispatch("search_files", {"query": "converged", "everyone": True}, _ctx("U0SAM"))
    assert "were NOT searched" in out


def test_a_root_is_scanned_once_even_when_several_users_share_it(sut, multi):
    """Everyone without their own root shares the default — scanning it per user
    would multiply the work for no extra coverage."""
    sut.registry.save(sut.registry.users() + [
        {"slack_user_id": "U0A", "alias": "a", "display_name": "A", "status": "active"},
        {"slack_user_id": "U0B", "alias": "b", "display_name": "B", "status": "active"},
    ])
    paths = [s["path"] for s in sut.tools._distinct_scopes()]
    assert len(paths) == len(set(str(p) for p in paths))


# --------------------------------------------------------------------------- #
# Writes
# --------------------------------------------------------------------------- #
def test_metadata_for_your_own_project_lands_in_the_sidecar(sut, multi):
    out = sut.dispatch("write_metadata",
                       {"project": "thermolysin", "content": "libraries: numpy"},
                       _ctx("U0SAM"))
    assert "Created metadata" in out
    assert (sut.METADATA_ROOT / "sam__U0SAM" / "thermolysin" / "metadata.md").is_file()
    # and nothing at all was added to the calculations tree
    assert sorted(p.name for p in (multi["sam"] / "thermolysin").iterdir()) == ["notes.txt"]


def test_you_cannot_write_metadata_for_someone_elses_project(sut, multi):
    out = sut.dispatch("write_metadata",
                       {"project": "dft", "content": "mine now", "owner": "arun"},
                       _ctx("U0SAM"))
    assert "belong to them" in out
    assert not (sut.METADATA_ROOT / "arun__U01ARUN" / "dft" / "metadata.md").exists()


def test_you_can_read_someone_elses_metadata(sut, multi):
    sut.dispatch("write_metadata", {"project": "dft", "content": "arun's notes"},
                 _ctx("U01ARUN"))
    out = sut.dispatch("read_metadata", {"project": "dft", "owner": "arun"}, _ctx("U0SAM"))
    assert "arun's notes" in out


def test_shared_project_metadata_is_writable_by_the_group(sut, multi):
    out = sut.dispatch("write_metadata",
                       {"project": "csd", "content": "group notes", "owner": "smb"},
                       _ctx("U01ARUN"))
    assert "Created metadata" in out
    assert (sut.METADATA_ROOT / "_shared__smb" / "csd" / "metadata.md").is_file()


def test_metadata_announces_itself_in_a_listing(sut, multi):
    sut.dispatch("write_metadata", {"project": "thermolysin", "content": "notes"},
                 _ctx("U0SAM"))
    out = sut.dispatch("list_directory", {"path": "thermolysin"}, _ctx("U0SAM"))
    assert "read_metadata" in out


def test_metadata_cannot_be_written_for_a_project_that_does_not_exist(sut, multi):
    out = sut.dispatch("write_metadata", {"project": "ghost", "content": "x"}, _ctx("U0SAM"))
    assert "does not exist" in out


def test_run_python_analysis_passes_the_owner_name_not_a_path(sut, multi, monkeypatch):
    """The tool server does its own registry lookup; a path never crosses the wire."""
    seen = {}

    class FakeResp:
        status_code = 200
        is_success = True

        def json(self):
            return {"status": "success", "duration_seconds": 0.1, "stdout": "", "figures": []}

    def fake_post(path, payload, timeout):
        seen.update({"path": path, "payload": payload})
        return FakeResp()

    monkeypatch.setattr(sut, "_tool_server_post", fake_post)
    monkeypatch.setattr(sut, "AGENT_INTERNAL_SECRET", "s")
    sut.dispatch("run_python_analysis",
                 {"project_name": "dft", "code": "print(1)", "dataset": ["r"],
                  "question": "q", "owner": "arun"},
                 _ctx("U0SAM"))
    assert seen["payload"]["owner"] == "arun"
    assert seen["payload"]["user_id"] == "U0SAM"
    assert not any(str(multi["arun"]) in str(v) for v in seen["payload"].values())
