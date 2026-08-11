"""
Tests for per-user calculations roots.

The properties that carry the weight:

* **Names, not paths.** Tools take an owner *name*; the registry turns it into a
  directory. Nothing a conversation can say resolves to an arbitrary path.
* **The fence follows the root.** Every root is its own boundary, and traversal
  out of one never lands in another.
* **Flat reads, owned writes.** Everyone may read every root; only the owner's
  metadata is theirs to change.
* **Nesting is refused.** Containment is how the fence works, so a root inside a
  root is not a boundary at all.
"""

import pytest


@pytest.fixture
def multi(sut, tmp_path, monkeypatch):
    """Three users with three separate roots, plus a shared one."""
    monkeypatch.setattr(sut, "USERS_FILE", tmp_path / "users.json")
    sam_root = tmp_path / "sam-calcs"
    arun_root = tmp_path / "arun-calcs"
    shared_root = tmp_path / "group-calcs"
    for root in (sam_root, arun_root, shared_root):
        root.mkdir()
    monkeypatch.setattr(sut, "SHARED_CALC_ROOTS", {"smb": str(shared_root)})
    monkeypatch.setattr(sut, "METADATA_ROOT", tmp_path / "metadata")
    monkeypatch.setattr(sut, "METADATA_HISTORY_ROOT", tmp_path / "metadata_history")

    sut.registry.invalidate()
    sut.registry.save([
        {"slack_user_id": "U0SAM", "alias": "sam", "display_name": "Sam",
         "role": "admin", "status": "active", "calc_root": str(sam_root)},
        {"slack_user_id": "U01ARUN", "alias": "arun", "display_name": "Arun N.",
         "role": "member", "status": "active", "calc_root": str(arun_root)},
        # No calc_root: falls back to the shared default, which is the migration
        # story — nothing changes for anyone until a root is set.
        {"slack_user_id": "U0PRIYA", "alias": "priya", "display_name": "Priya P.",
         "role": "member", "status": "active"},
    ])
    yield {"sam": sam_root, "arun": arun_root, "shared": shared_root, "tmp": tmp_path}
    sut.registry.invalidate()


# --------------------------------------------------------------------------- #
# Resolution
# --------------------------------------------------------------------------- #
def test_a_bare_path_means_the_speakers_own_files(sut, multi):
    (multi["sam"] / "thermolysin").mkdir()
    path, scope, err = sut.roots.resolve("thermolysin", "", "U0SAM")
    assert not err
    assert path == multi["sam"] / "thermolysin"
    assert scope["name"] == "sam"


def test_owner_names_someone_elses_root(sut, multi):
    (multi["arun"] / "dft").mkdir()
    path, scope, err = sut.roots.resolve("dft", "arun", "U0SAM")
    assert not err and path == multi["arun"] / "dft"
    assert scope["name"] == "arun"


def test_an_at_prefix_is_the_same_thing(sut, multi):
    (multi["arun"] / "dft").mkdir()
    by_prefix, _, _ = sut.roots.resolve("@arun/dft", "", "U0SAM")
    by_owner, _, _ = sut.roots.resolve("dft", "arun", "U0SAM")
    assert by_prefix == by_owner


def test_a_slack_id_resolves_like_an_alias(sut, multi):
    path, _scope, err = sut.roots.resolve(".", "U01ARUN", "U0SAM")
    assert not err and path == multi["arun"]


def test_shared_roots_are_addressable_by_name(sut, multi):
    (multi["shared"] / "csd").mkdir()
    path, scope, err = sut.roots.resolve("csd", "smb", "U0SAM")
    assert not err and path == multi["shared"] / "csd"
    assert scope["kind"] == "shared"


def test_a_user_without_a_root_falls_back_to_the_default(sut, multi):
    path, _scope, err = sut.roots.resolve(".", "", "U0PRIYA")
    assert not err and path == sut.CALCULATIONS_ROOT


def test_prefix_and_owner_disagreeing_is_an_error(sut, multi):
    _path, _scope, err = sut.roots.resolve("@arun/dft", "sam", "U0SAM")
    assert "not both" in err


def test_an_unknown_owner_lists_the_options(sut, multi):
    _path, _scope, err = sut.roots.resolve("x", "nobody", "U0SAM")
    assert "no root named" in err and "@arun" in err


# --------------------------------------------------------------------------- #
# Fencing
# --------------------------------------------------------------------------- #
def test_traversal_cannot_escape_a_root(sut, multi):
    _path, _scope, err = sut.roots.resolve("../arun-calcs/secret", "", "U0SAM")
    assert "outside the allowed directory" in err


def test_traversal_cannot_hop_between_roots(sut, multi):
    """Even naming a real neighbouring root by path is refused — names only."""
    _path, _scope, err = sut.roots.resolve("../../etc/passwd", "arun", "U0SAM")
    assert "outside the allowed directory" in err


def test_an_absolute_path_is_refused(sut, multi):
    _path, _scope, err = sut.roots.resolve("/etc/passwd", "", "U0SAM")
    assert "outside the allowed directory" in err


def test_a_symlink_out_of_the_root_is_refused(sut, multi):
    (multi["sam"] / "escape").symlink_to(multi["arun"])
    _path, _scope, err = sut.roots.resolve("escape", "", "U0SAM")
    assert "outside the allowed directory" in err


# --------------------------------------------------------------------------- #
# Display
# --------------------------------------------------------------------------- #
def test_paths_come_back_qualified_with_their_owner(sut, multi):
    assert sut.roots.qualify("arun", "thermolysin/run") == "@arun/thermolysin/run"
    assert sut.roots.qualify("arun", ".") == "@arun"
    assert sut.roots.qualify("", "thermolysin") == "thermolysin"


def test_the_preamble_names_the_other_roots(sut, multi):
    lines = "\n".join(sut.roots.preamble_lines("U0SAM"))
    assert "@arun" in lines and "@smb" in lines
    assert "READ every root" in lines


def test_the_preamble_groups_users_who_share_one_tree(sut, multi):
    """Listing an alias per person would imply trees that don't exist."""
    users = sut.registry.users() + [
        {"slack_user_id": f"U0X{i}", "alias": f"x{i}", "display_name": f"X{i}",
         "status": "active"} for i in range(6)
    ]
    sut.registry.save(users)
    lines = "\n".join(sut.roots.preamble_lines("U0SAM"))
    assert "all the same shared tree" in lines
    assert "+" in lines and "more" in lines          # the tail is counted, not listed


def test_the_preamble_says_who_shares_the_speakers_own_tree(sut, multi):
    """Priya has no root of her own, so an unqualified path covers her work too."""
    lines = "\n".join(sut.roots.preamble_lines("U0PRIYA"))
    assert "read the same tree as the speaker" in lines or "@priya" in lines


# --------------------------------------------------------------------------- #
# Validation
# --------------------------------------------------------------------------- #
def test_validate_rejects_a_missing_directory(sut, multi):
    assert "does not exist" in sut.roots.validate(str(multi["tmp"] / "nope"))


def test_validate_rejects_a_file(sut, multi):
    f = multi["tmp"] / "afile"
    f.write_text("x")
    assert "not a directory" in sut.roots.validate(str(f))


def test_validate_rejects_a_root_inside_another(sut, multi):
    nested = multi["arun"] / "inner"
    nested.mkdir()
    problem = sut.roots.validate(str(nested), for_uid="U0SAM")
    assert "nested with" in problem


def test_validate_rejects_a_root_that_contains_another(sut, multi):
    problem = sut.roots.validate(str(multi["tmp"]), for_uid="U0SAM")
    assert "nested with" in problem


def test_validate_rejects_the_state_directory(sut, multi, monkeypatch):
    monkeypatch.setattr(sut, "STATE_DIR", multi["tmp"] / "state")
    (multi["tmp"] / "state").mkdir()
    assert "overlaps" in sut.roots.validate(str(multi["tmp"] / "state"), for_uid="U0SAM")


def test_validate_accepts_setting_your_own_root_again(sut, multi):
    """Re-pointing someone at their current root is not a self-collision."""
    assert sut.roots.validate(str(multi["sam"]), for_uid="U0SAM") is None


def test_check_all_is_quiet_when_the_roots_are_sane(sut, multi):
    assert sut.roots.check_all() == []


def test_check_all_reports_a_missing_root(sut, multi):
    import shutil
    shutil.rmtree(multi["arun"])
    problems = sut.roots.check_all()
    assert any("@arun" in p for p in problems)


# --------------------------------------------------------------------------- #
# aspen-users set-root
#
# Validation lives at write time as well as at boot, so a bad path fails in front
# of the admin typing it rather than in somebody's Slack thread later.
# --------------------------------------------------------------------------- #
@pytest.fixture
def cli(sut):
    import importlib
    return importlib.import_module("aspen.users_cli")


def test_set_root_points_a_user_at_their_tree(sut, multi, cli):
    new_root = multi["tmp"] / "priya-calcs"
    new_root.mkdir()
    assert cli.main(["set-root", "priya", str(new_root)]) == 0
    assert sut.roots.for_user("U0PRIYA") == new_root


def test_set_root_records_the_unix_account(sut, multi, cli):
    new_root = multi["tmp"] / "priya2"
    new_root.mkdir()
    cli.main(["set-root", "priya", str(new_root), "--unix-user", "ppatel"])
    assert sut.registry.by_id("U0PRIYA")["unix_user"] == "ppatel"


def test_set_root_refuses_a_missing_directory(sut, multi, cli):
    assert cli.main(["set-root", "priya", str(multi["tmp"] / "ghost")]) == 1
    assert sut.roots.for_user("U0PRIYA") == sut.CALCULATIONS_ROOT


def test_set_root_refuses_a_nested_root(sut, multi, cli, capsys):
    nested = multi["arun"] / "inner"
    nested.mkdir()
    assert cli.main(["set-root", "priya", str(nested)]) == 1
    assert "nested with" in capsys.readouterr().err


def test_set_root_refuses_an_unknown_user(sut, multi, cli):
    assert cli.main(["set-root", "nobody", str(multi["tmp"])]) == 1


def test_set_root_needs_a_path_or_clear(sut, multi, cli, capsys):
    assert cli.main(["set-root", "priya"]) == 1
    assert "--clear" in capsys.readouterr().err


def test_clear_falls_back_to_the_default(sut, multi, cli):
    assert cli.main(["set-root", "arun", "--clear"]) == 0
    assert sut.roots.for_user("U01ARUN") == sut.CALCULATIONS_ROOT


def test_roots_command_lists_every_root(sut, multi, cli, capsys):
    assert cli.main(["roots"]) == 0
    out = capsys.readouterr().out
    assert "@sam" in out and "@arun" in out and "@smb" in out
    assert "shared" in out
