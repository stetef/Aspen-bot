"""
Tests for the user registry: alias rules, admission, hot reload, and the
``aspen-users`` CLI.

The property that matters most here is that **the registry is the admission
boundary and it reloads without a restart** — revoking someone has to take effect
on their next message, not at the next deploy. Alias handling is the other half:
aliases are for humans, IDs authorize, and the two must never swap roles.
"""

from unittest.mock import MagicMock

import pytest


@pytest.fixture
def reg(sut, env):
    """Point config at a scratch registry + workflows tree, and yield helpers."""
    return sut.registry


def _u(uid, alias, name=None, role="member", status="active"):
    return {
        "slack_user_id": uid, "alias": alias, "display_name": name or alias,
        "role": role, "status": status, "added": "2026-08-01", "added_by": "test",
        "removed": "", "removed_by": "", "notes": "",
    }


@pytest.fixture
def no_op_agent(sut, monkeypatch):
    async def _fake_handle(key, user_message, context):
        return "reply!", []
    monkeypatch.setattr(sut.MANAGER, "handle", _fake_handle)


def _event(user, text="hello"):
    return {"user": user, "text": text, "channel": "C", "ts": "1.0",
            "channel_type": "channel"}


# --------------------------------------------------------------------------- #
# Aliases
# --------------------------------------------------------------------------- #
def test_slugify_makes_kebab_case(sut):
    assert sut.registry.slugify("Arun N.") == "arun-n"
    assert sut.registry.slugify("Mary-Jane  O'Brien") == "mary-jane-o-brien"


def test_slugify_strips_accents(sut):
    assert sut.registry.slugify("José Ramírez") == "jose-ramirez"


@pytest.mark.parametrize("alias", ["_group", "_archive", "Arun", "arun_n", "a--b",
                                   "-arun", "arun-", "", "x" * 33])
def test_bad_aliases_are_rejected(sut, alias):
    assert sut.registry.validate_alias(alias) is not None


def test_reserved_aliases_are_rejected(sut):
    # The leading-underscore forms are unreachable by the grammar; these are the
    # bare words that would still be confusing as folder names.
    assert sut.registry.validate_alias("group") is not None
    assert sut.registry.validate_alias("archive") is not None


def test_good_aliases_are_accepted(sut):
    for alias in ("arun", "arun-n", "j2", "mary-jane-o-brien"):
        assert sut.registry.validate_alias(alias) is None


def test_user_id_validation(sut):
    assert sut.registry.validate_user_id("U01ABC2DEF") is None
    assert sut.registry.validate_user_id("W01ABC2DEF") is None   # Enterprise Grid
    assert sut.registry.validate_user_id("arun") is not None
    assert sut.registry.validate_user_id("") is not None


# --------------------------------------------------------------------------- #
# Loading / admission
# --------------------------------------------------------------------------- #
def test_bootstrap_from_env_when_no_file(sut, reg):
    # No registry file -> the allowlist is ASPEN_ALLOWED_SLACK_USER_IDS, exactly
    # as it was before the registry existed.
    assert sut.ALLOWED_USER_IDS == {"U1", "U2", "U3", "U4", "U5"}
    assert sut.ADMIN_USER_ID == "U1"


def test_registry_file_supersedes_env_bootstrap(sut, reg):
    reg.save([_u("U0SAM", "sam", "Sam", role="admin"), _u("U01ARUN", "arun", "Arun N.")])
    assert sut.ALLOWED_USER_IDS == {"U0SAM", "U01ARUN"}
    assert "U1" not in sut.ALLOWED_USER_IDS       # the env bootstrap is gone


def test_removed_user_is_not_in_the_allowlist(sut, reg):
    reg.save([_u("U0SAM", "sam"), _u("U01ARUN", "arun", status="removed")])
    assert sut.ALLOWED_USER_IDS == {"U0SAM"}


def test_allowlist_change_applies_without_restart(sut, reg):
    reg.save([_u("U0SAM", "sam"), _u("U01ARUN", "arun")])
    assert "U01ARUN" in sut.ALLOWED_USER_IDS
    # Same process, no re-import: the mtime changes and the next read reloads.
    reg.save([_u("U0SAM", "sam"), _u("U01ARUN", "arun", status="removed")])
    assert "U01ARUN" not in sut.ALLOWED_USER_IDS


def test_removed_user_is_refused_on_their_next_message(sut, reg, say, no_op_agent):
    """The end-to-end property: revoking access takes effect immediately."""
    reg.save([_u("U0SAM", "sam"), _u("U01ARUN", "arun")])
    sut._handle_event(_event("U01ARUN"), say, MagicMock(), strip_mention=False)
    assert "reply!" in say.texts

    reg.save([_u("U0SAM", "sam"), _u("U01ARUN", "arun", status="removed")])
    say.texts.clear()
    sut._handle_event(_event("U01ARUN"), say, MagicMock(), strip_mention=False)
    assert "reply!" not in say.texts
    assert any("not authorized" in t for t in say.texts)


def test_malformed_registry_keeps_the_last_good_copy(sut, reg):
    reg.save([_u("U0SAM", "sam"), _u("U01ARUN", "arun")])
    assert sut.ALLOWED_USER_IDS == {"U0SAM", "U01ARUN"}
    # Corrupt the file. Access must NOT change — never widen, never lock out.
    sut.USERS_FILE.write_text("{ this is not json", encoding="utf-8")
    assert sut.ALLOWED_USER_IDS == {"U0SAM", "U01ARUN"}


def test_malformed_registry_with_no_cache_falls_back_to_env(sut, reg):
    sut.USERS_FILE.write_text("}{", encoding="utf-8")
    reg.invalidate()                       # simulate a fresh process
    # The env bootstrap is operator-controlled, so this can't grant more than the
    # operator configured — and it keeps a typo from locking the admin out.
    assert sut.ALLOWED_USER_IDS == {"U1", "U2", "U3", "U4", "U5"}


def test_entries_with_bad_ids_are_dropped_not_fatal(sut, reg):
    reg.save([_u("U0SAM", "sam"), _u("not-an-id", "bogus"), _u("U01ARUN", "arun")])
    assert sut.ALLOWED_USER_IDS == {"U0SAM", "U01ARUN"}


def test_duplicate_alias_is_normalized_away(sut, reg):
    reg.save([_u("U0SAM", "sam"), _u("U01ARUN", "sam")])
    # Both users survive; the loser gets a fallback alias derived from their ID.
    assert sut.ALLOWED_USER_IDS == {"U0SAM", "U01ARUN"}
    assert reg.by_id("U01ARUN")["alias"] == "u01arun"


# --------------------------------------------------------------------------- #
# Resolution
# --------------------------------------------------------------------------- #
def test_resolve_by_alias_and_by_id(sut, reg):
    reg.save([_u("U01ARUN", "arun", "Arun N.")])
    assert reg.resolve("arun")["slack_user_id"] == "U01ARUN"
    assert reg.resolve("U01ARUN")["alias"] == "arun"
    assert reg.resolve("@arun")["slack_user_id"] == "U01ARUN"
    assert reg.resolve("nobody") is None


def test_alias_lookup_never_authorizes(sut, reg, say, no_op_agent):
    """resolve() finds users; admission is a separate ID check. Sending the alias
    as the Slack user ID must not get you in."""
    reg.save([_u("U01ARUN", "arun", "Arun N.")])
    sut._handle_event(_event("arun"), say, MagicMock(), strip_mention=False)
    assert any("not authorized" in t for t in say.texts)


def test_admin_is_the_role_admin(sut, reg):
    reg.save([_u("U01ARUN", "arun"), _u("U0SAM", "sam", role="admin")])
    assert sut.ADMIN_USER_ID == "U0SAM"          # not merely the first entry


def test_admin_falls_back_to_first_active_user(sut, reg):
    reg.save([_u("U01ARUN", "arun"), _u("U0SAM", "sam")])
    assert sut.ADMIN_USER_ID == "U01ARUN"


def test_admin_env_override_wins(sut, reg, monkeypatch):
    monkeypatch.setattr(sut, "ADMIN_OVERRIDE", "U0OVERRIDE")
    reg.save([_u("U01ARUN", "arun"), _u("U0SAM", "sam", role="admin")])
    assert sut.ADMIN_USER_ID == "U0OVERRIDE"


def test_label_is_human_readable(sut, reg):
    reg.save([_u("U01ARUN", "arun", "Arun N.")])
    assert reg.label("U01ARUN") == "Arun N. (arun)"
    assert reg.label("U0NOBODY") == "U0NOBODY"


# --------------------------------------------------------------------------- #
# Startup placement guard
# --------------------------------------------------------------------------- #
def test_startup_accepts_a_sane_layout(sut, reg):
    import aspen.main  # noqa: F401  (the facade needs the module imported)
    sut._check_state_locations()          # must not raise
    assert sut.WORKFLOWS_ROOT.is_dir()    # and it creates the tree


def test_startup_refuses_a_registry_inside_the_workspace(sut, reg, monkeypatch):
    """Ownership and admission are enforced in Python — which only holds if the
    files aren't reachable through a path sandboxed code can write."""
    import aspen.main  # noqa: F401
    monkeypatch.setattr(sut, "WORKSPACE_ROOT", sut.USERS_FILE.parent)
    with pytest.raises(SystemExit, match="sandbox-writable"):
        sut._check_state_locations()


def test_startup_refuses_workflows_inside_a_sandbox_write_path(sut, reg, monkeypatch):
    import aspen.main  # noqa: F401
    from aspen import config
    monkeypatch.setattr(config, "SANDBOX_WRITE_PATHS", [str(sut.WORKFLOWS_ROOT.parent)])
    with pytest.raises(SystemExit, match="sandbox-writable"):
        sut._check_state_locations()


# --------------------------------------------------------------------------- #
# The aspen-users CLI
# --------------------------------------------------------------------------- #
@pytest.fixture
def cli(sut, reg, monkeypatch):
    """CLI against an empty registry — no bootstrap users to migrate."""
    monkeypatch.setattr(sut, "BOOTSTRAP_USER_IDS", [])
    reg.invalidate()
    from aspen import users_cli
    return users_cli


@pytest.fixture
def cli_bootstrap(sut, reg):
    """CLI with the env bootstrap still in place (U1…U5), for migration tests."""
    from aspen import users_cli
    return users_cli


def test_cli_first_write_migrates_the_bootstrap(cli_bootstrap, sut, reg, capsys):
    """The first `add` must not silently drop the bootstrap users (mass
    revocation) nor absorb them with junk aliases — it migrates them explicitly."""
    cli = cli_bootstrap
    assert sut.ALLOWED_USER_IDS == {"U1", "U2", "U3", "U4", "U5"}
    cli.main(["add", "U01ARUN", "--alias", "arun", "--name", "Arun N."])
    out = capsys.readouterr().out
    assert "migrating 5 user(s)" in out          # announced, not silent
    assert sut.ALLOWED_USER_IDS == {"U1", "U2", "U3", "U4", "U5", "U01ARUN"}
    assert reg.load()["source"] == "file"


def test_bootstrap_migration_keeps_the_first_id_as_admin(cli_bootstrap, sut, reg):
    cli = cli_bootstrap
    cli.main(["init"])
    assert sut.ADMIN_USER_ID == "U1"
    assert reg.by_id("U1")["role"] == "admin"


def test_init_is_idempotent(cli_bootstrap, sut, reg, capsys):
    cli = cli_bootstrap
    cli.main(["init"])
    capsys.readouterr()
    assert cli.main(["init"]) == 0
    assert "already exists" in capsys.readouterr().out


def test_migration_runs_only_once(cli_bootstrap, sut, reg):
    cli = cli_bootstrap
    cli.main(["add", "U01ARUN", "--alias", "arun", "--name", "Arun N."])
    cli.main(["remove", "u1", "-y", "--force"])
    # A second write must not resurrect the bootstrap users it just removed.
    cli.main(["add", "U0PRIYA", "--alias", "priya", "--name", "Priya"])
    assert "U1" not in sut.ALLOWED_USER_IDS


def test_cli_add_registers_a_user(cli, sut, reg, capsys):
    assert cli.main(["add", "U01ARUN", "--alias", "arun", "--name", "Arun N."]) == 0
    assert "U01ARUN" in sut.ALLOWED_USER_IDS
    # --name is slugified into the alias; the stored name then comes back FROM
    # the alias, so a Slack handle can never be recorded as somebody's name.
    assert reg.by_alias("arun")["display_name"] == "Arun"


def test_cli_add_derives_the_alias_from_the_name(cli, reg):
    cli.main(["add", "U01ARUN", "--name", "Arun N."])
    assert reg.by_id("U01ARUN")["alias"] == "arun-n"


def test_cli_add_rejects_a_duplicate_alias(cli, reg, capsys):
    cli.main(["add", "U0SAM", "--alias", "sam", "--name", "Sam"])
    assert cli.main(["add", "U01ARUN", "--alias", "sam", "--name", "Arun"]) == 1
    assert "already used" in capsys.readouterr().err


def test_cli_add_rejects_a_bad_slack_id(cli, capsys):
    assert cli.main(["add", "arun", "--alias", "arun"]) == 1
    assert "Slack member ID" in capsys.readouterr().err


def test_cli_remove_revokes_access(cli, sut, reg):
    cli.main(["add", "U0SAM", "--alias", "sam", "--name", "Sam"])
    cli.main(["add", "U01ARUN", "--alias", "arun", "--name", "Arun"])
    assert cli.main(["remove", "arun", "-y"]) == 0
    assert "U01ARUN" not in sut.ALLOWED_USER_IDS
    assert reg.by_id("U01ARUN")["status"] == "removed"


def test_cli_remove_by_name_alias(cli, sut, reg):
    cli.main(["add", "U0SAM", "--alias", "sam", "--name", "Sam"])
    cli.main(["add", "U01ARUN", "--alias", "arun-n", "--name", "Arun N."])
    assert cli.main(["remove", "arun-n", "-y"]) == 0
    assert "U01ARUN" not in sut.ALLOWED_USER_IDS


def test_cli_remove_protects_the_admin(cli, sut, reg, capsys):
    cli.main(["add", "U0SAM", "--alias", "sam", "--name", "Sam", "--role", "admin"])
    assert cli.main(["remove", "sam", "-y"]) == 1
    assert "admin" in capsys.readouterr().err
    assert "U0SAM" in sut.ALLOWED_USER_IDS


def test_cli_remove_admin_with_force(cli, sut, reg):
    cli.main(["add", "U0SAM", "--alias", "sam", "--name", "Sam", "--role", "admin"])
    assert cli.main(["remove", "sam", "-y", "--force"]) == 0
    assert "U0SAM" not in sut.ALLOWED_USER_IDS


def test_cli_add_reinstates_a_removed_user(cli, sut, reg):
    cli.main(["add", "U0SAM", "--alias", "sam", "--name", "Sam"])
    cli.main(["add", "U01ARUN", "--alias", "arun", "--name", "Arun"])
    cli.main(["remove", "arun", "-y"])
    assert cli.main(["add", "U01ARUN", "--alias", "arun", "--name", "Arun"]) == 0
    assert "U01ARUN" in sut.ALLOWED_USER_IDS


def test_cli_rename_changes_the_alias(cli, sut, reg):
    cli.main(["add", "U01ARUN", "--alias", "arun", "--name", "Arun"])
    assert cli.main(["rename", "arun", "--to", "arun-n"]) == 0
    assert reg.by_id("U01ARUN")["alias"] == "arun-n"
    assert reg.by_alias("arun") is None


def test_cli_rename_rejects_a_taken_alias(cli, reg, capsys):
    cli.main(["add", "U0SAM", "--alias", "sam", "--name", "Sam"])
    cli.main(["add", "U01ARUN", "--alias", "arun", "--name", "Arun"])
    assert cli.main(["rename", "arun", "--to", "sam"]) == 1
    assert "already used" in capsys.readouterr().err


def test_display_name_is_derived_from_the_alias(reg):
    assert reg.display_from_alias("macon-abernathy") == "Macon Abernathy"
    assert reg.display_from_alias("arun-asundi") == "Arun Asundi"
    assert reg.display_from_alias("sam-tetef") == "Sam Tetef"
    assert reg.display_from_alias("") == ""


def test_display_name_does_not_re_spell_a_name_it_only_capitalises(reg):
    """Only the first letter of each part — a rule-based title-caser would turn
    "o'brien" into "O'Brien" and "mcdonald" into "McDonald" and be wrong as often
    as it is right."""
    assert reg.display_from_alias("kelly-o'brien") == "Kelly O'brien"
    assert reg.display_from_alias("ada-mcdonald") == "Ada Mcdonald"


def test_renaming_someone_updates_the_name_that_follows_from_it(cli, reg):
    cli.main(["add", "U0X", "--alias", "mjabern", "--name", "mjabern"])
    assert reg.by_alias("mjabern")["display_name"] == "Mjabern"
    cli.main(["rename", "mjabern", "--to", "macon-abernathy"])
    assert reg.by_alias("macon-abernathy")["display_name"] == "Macon Abernathy"


def test_cli_whois_reports_the_entry(cli, reg, capsys):
    cli.main(["add", "U01ARUN", "--alias", "arun-n", "--name", "Arun N."])
    assert cli.main(["whois", "arun-n"]) == 0
    out = capsys.readouterr().out
    assert "U01ARUN" in out and "Arun N" in out


def test_cli_list_shows_registered_users(cli, reg, capsys):
    cli.main(["add", "U01ARUN", "--alias", "arun", "--name", "Arun N."])
    assert cli.main(["list"]) == 0
    out = capsys.readouterr().out
    assert "arun" in out and "U01ARUN" in out


def test_cli_list_hides_removed_users_unless_asked(cli, reg, capsys):
    cli.main(["add", "U0SAM", "--alias", "sam", "--name", "Sam"])
    cli.main(["add", "U01ARUN", "--alias", "arun", "--name", "Arun"])
    cli.main(["remove", "arun", "-y"])
    capsys.readouterr()
    cli.main(["list"])
    assert "arun" not in capsys.readouterr().out
    cli.main(["list", "--all"])
    assert "arun" in capsys.readouterr().out


# --------------------------------------------------------------------------- #
# The hot-reload hook, and the way tests can break it
# --------------------------------------------------------------------------- #
def test_the_allowlist_is_served_by_the_hook_not_a_stored_attribute(sut):
    """Hot reload only works while the name is ABSENT from config.__dict__."""
    import importlib
    config = importlib.import_module("aspen.config")
    assert "ALLOWED_USER_IDS" not in config.__dict__
    assert "ADMIN_USER_ID" not in config.__dict__


def test_monkeypatching_the_allowlist_would_freeze_it_without_the_cleanup(sut):
    """Pins the hazard the conftest cleanup exists for.

    ``monkeypatch.undo()`` restores by ``setattr``, which turns a hook-backed name
    into a real attribute frozen at the value it had when the patch was applied —
    so the allowlist stops tracking the registry for every later test. This walks
    that sequence by hand and shows the cleanup is what repairs it.
    """
    import importlib
    from tests.conftest import _unshadow_hook_backed

    config = importlib.import_module("aspen.config")
    live = set(config.ALLOWED_USER_IDS)

    original = getattr(config, "ALLOWED_USER_IDS")        # what monkeypatch captures
    setattr(config, "ALLOWED_USER_IDS", {"UTEST"})        # ... setattr
    setattr(config, "ALLOWED_USER_IDS", original)         # ... and undo()

    assert "ALLOWED_USER_IDS" in config.__dict__          # the hook is now shadowed
    _unshadow_hook_backed()
    assert "ALLOWED_USER_IDS" not in config.__dict__
    assert set(config.ALLOWED_USER_IDS) == live           # tracking the registry again


def test_the_cleanup_runs_between_tests(sut, monkeypatch):
    """The real path: monkeypatch it here, and the autouse fixture repairs it."""
    monkeypatch.setattr(sut, "ALLOWED_USER_IDS", {"UONLY"})
    assert sut.ALLOWED_USER_IDS == {"UONLY"}
    # The assertion for the next test is in test_the_allowlist_survives_a_patch below.


def test_the_allowlist_survives_a_patch_in_a_previous_test(sut):
    import importlib
    config = importlib.import_module("aspen.config")
    assert "ALLOWED_USER_IDS" not in config.__dict__
    assert "UONLY" not in config.ALLOWED_USER_IDS
