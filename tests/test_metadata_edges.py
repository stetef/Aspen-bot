"""
Edge paths in the metadata sidecar and the read tools.

Everything here is a failure or a migration path — the code that only runs when
something has already gone sideways, and therefore the code most likely to be
wrong. A backup that raises would block a write; a rename that misses would
orphan someone's notes; a permission error that escapes would surface to a
scientist as a stack trace instead of a sentence.
"""

import os

import pytest


@pytest.fixture
def meta(sut, env):
    """Arun with a root and one project, ready to be annotated."""
    root = env.root("arun-calcs", ["dft"])
    env.register(
        {"slack_user_id": "U01ARUN", "alias": "arun", "display_name": "Arun N.",
         "role": "admin", "calc_root": str(root)},
    )
    return {"root": root, "env": env}


def _ctx(uid="U01ARUN"):
    return {"user_id": uid, "username": "", "thread_ts": "1.0", "attachments": []}


# --------------------------------------------------------------------------- #
# Following an alias rename
# --------------------------------------------------------------------------- #
def test_notes_follow_their_owner_through_a_rename(sut, meta):
    """Directories carry the alias for humans; lookups go by ID, so a rename
    must move the folder rather than orphan it."""
    sut.dispatch("write_metadata", {"project": "dft", "content": "before"}, _ctx())
    assert (sut.METADATA_ROOT / "arun__U01ARUN" / "dft" / "metadata.md").is_file()

    sut.registry.save([dict(u, alias="arun-n") for u in sut.registry.users()])
    out = sut.dispatch("read_metadata", {"project": "dft"}, _ctx())

    assert "before" in out
    assert (sut.METADATA_ROOT / "arun-n__U01ARUN" / "dft" / "metadata.md").is_file()
    assert not (sut.METADATA_ROOT / "arun__U01ARUN").exists()


def test_an_unmovable_directory_still_reads(sut, meta, monkeypatch):
    """If the rename fails, fall back to what's on disk — never lose the notes."""
    sut.dispatch("write_metadata", {"project": "dft", "content": "durable"}, _ctx())

    from pathlib import Path
    def _refuse(self, target):
        raise OSError("read-only filesystem")
    monkeypatch.setattr(Path, "rename", _refuse)

    sut.registry.save([dict(u, alias="arun-n") for u in sut.registry.users()])
    assert "durable" in sut.dispatch("read_metadata", {"project": "dft"}, _ctx())


# --------------------------------------------------------------------------- #
# Backups
# --------------------------------------------------------------------------- #
def test_a_failed_backup_never_blocks_the_write(sut, meta, monkeypatch):
    sut.dispatch("write_metadata", {"project": "dft", "content": "v1"}, _ctx())
    monkeypatch.setattr(sut.metadata.shutil, "copy2",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("disk full")))
    out = sut.dispatch("write_metadata", {"project": "dft", "content": "v2"}, _ctx())
    assert "Updated" in out
    sidecar = sut.METADATA_ROOT / "arun__U01ARUN" / "dft" / "metadata.md"
    assert sidecar.read_text() == "v2"


def test_repeat_writes_in_the_same_second_do_not_collide(sut, meta):
    for i in range(3):
        sut.dispatch("write_metadata", {"project": "dft", "content": f"v{i}"}, _ctx())
    history = sut.METADATA_HISTORY_ROOT / "arun__U01ARUN" / "dft"
    assert len(list(history.glob("*.md"))) == 2      # two overwrites, two snapshots


# --------------------------------------------------------------------------- #
# Reading
# --------------------------------------------------------------------------- #
def test_an_unreadable_note_reports_rather_than_raises(sut, meta):
    sut.dispatch("write_metadata", {"project": "dft", "content": "secret"}, _ctx())
    sidecar = sut.METADATA_ROOT / "arun__U01ARUN" / "dft" / "metadata.md"
    original = os.stat(sidecar).st_mode
    os.chmod(sidecar, 0o000)
    try:
        if os.access(sidecar, os.R_OK):
            pytest.skip("running as a user that ignores mode bits (root?)")
        out = sut.dispatch("read_metadata", {"project": "dft"}, _ctx())
        assert out.startswith("Error:") and "could not read" in out
    finally:
        os.chmod(sidecar, original)


def test_reading_notes_for_a_project_that_does_not_exist(sut, meta):
    out = sut.dispatch("read_metadata", {"project": "ghost"}, _ctx())
    assert "does not exist" in out


def test_a_nested_project_name_is_refused_on_read(sut, meta):
    assert "not a valid project name" in sut.dispatch(
        "read_metadata", {"project": "dft/sub"}, _ctx())


def test_the_listing_hint_only_appears_when_notes_exist(sut, meta):
    before = sut.dispatch("list_directory", {"path": "dft"}, _ctx())
    assert "read_metadata" not in before
    sut.dispatch("write_metadata", {"project": "dft", "content": "notes"}, _ctx())
    after = sut.dispatch("list_directory", {"path": "dft"}, _ctx())
    assert "read_metadata" in after


def test_an_empty_note_is_refused(sut, meta):
    assert "empty" in sut.dispatch("write_metadata",
                                   {"project": "dft", "content": "   "}, _ctx())


# --------------------------------------------------------------------------- #
# Read tools: permission errors reach the user as sentences
# --------------------------------------------------------------------------- #
def test_an_unreadable_directory_is_reported(sut, meta):
    blocked = meta["root"] / "locked"
    blocked.mkdir()
    original = os.stat(blocked).st_mode
    os.chmod(blocked, 0o000)
    try:
        if os.access(blocked, os.R_OK):
            pytest.skip("running as a user that ignores mode bits (root?)")
        out = sut.dispatch("list_directory", {"path": "locked"}, _ctx())
        assert "permission denied" in out
    finally:
        os.chmod(blocked, original)


def test_an_unreadable_file_is_reported(sut, meta):
    blocked = meta["root"] / "dft" / "secret.out"
    blocked.write_text("x")
    original = os.stat(blocked).st_mode
    os.chmod(blocked, 0o000)
    try:
        if os.access(blocked, os.R_OK):
            pytest.skip("running as a user that ignores mode bits (root?)")
        out = sut.dispatch("read_file", {"path": "dft/secret.out"}, _ctx())
        assert "permission denied" in out
    finally:
        os.chmod(blocked, original)


# --------------------------------------------------------------------------- #
# Search edges
# --------------------------------------------------------------------------- #
def test_an_empty_query_is_refused(sut, meta):
    assert "empty" in sut.dispatch("search_files", {"query": ""}, _ctx())


def test_an_invalid_regex_is_reported_not_raised(sut, meta):
    out = sut.dispatch("search_files", {"query": "([unclosed", "regex": True}, _ctx())
    assert "invalid regular expression" in out


def test_searching_a_path_that_only_some_roots_have(sut, meta, env):
    """A cross-root sweep must skip roots without that subpath, not fail."""
    other = env.root("other-calcs", ["elsewhere"])
    (other / "elsewhere" / "x.txt").write_text("target here\n")
    (meta["root"] / "dft" / "y.txt").write_text("target here\n")
    env.register(
        {"slack_user_id": "U01ARUN", "alias": "arun", "display_name": "Arun N.",
         "role": "admin", "calc_root": str(meta["root"])},
        {"slack_user_id": "U0OTHER", "alias": "other", "display_name": "Other",
         "calc_root": str(other)},
    )
    out = sut.dispatch("search_files", {"query": "target", "path": "dft",
                                        "everyone": True}, _ctx())
    assert "@arun/dft/y.txt" in out
    assert "elsewhere" not in out


def test_searching_a_single_file_works(sut, meta):
    (meta["root"] / "dft" / "one.out").write_text("SCF converged\n")
    out = sut.dispatch("search_files", {"query": "converged", "path": "dft/one.out"},
                       _ctx())
    assert "one.out:1:" in out


def test_the_match_cap_is_announced(sut, meta, monkeypatch):
    monkeypatch.setattr(sut, "SEARCH_MAX_MATCHES", 2, raising=False)
    (meta["root"] / "dft" / "many.out").write_text("hit\n" * 10)
    out = sut.dispatch("search_files", {"query": "hit", "path": "dft"}, _ctx())
    assert "match limit" in out


def test_the_file_cap_is_announced(sut, meta, monkeypatch):
    monkeypatch.setattr(sut, "SEARCH_MAX_FILES", 1, raising=False)
    for i in range(4):
        (meta["root"] / "dft" / f"f{i}.out").write_text("nothing\n")
    out = sut.dispatch("search_files", {"query": "zzz", "path": "dft"}, _ctx())
    assert "1 file(s) searched" in out
