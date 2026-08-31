"""
Tests for the admin CLI paths that had no coverage: ``sync``, the request queue,
the setup view, and the telemetry switch.

These are the commands an operator reaches for when something is already wrong —
names drifted, someone is waiting, a collection window needs closing — so the
failure mode that matters is a command that *looks* like it worked. Each test
therefore checks the effect on disk, not just the exit code.
"""

import importlib

import pytest


@pytest.fixture
def cli(sut, env):
    env.register(
        {"slack_user_id": "U0SAM", "alias": "sam", "display_name": "Sam", "role": "admin"},
        {"slack_user_id": "U01ARUN", "alias": "arun", "display_name": "Arun N."},
    )
    return importlib.import_module("aspen.users_cli")


def _slack(cli, monkeypatch, names):
    """Stub the Slack profile lookup: {uid: display_name} (or an error string)."""
    def _lookup(uid):
        value = names.get(uid, "")
        return ("", value) if value.startswith("!") else (value, "")
    monkeypatch.setattr(cli, "_lookup_slack_profile", _lookup)


# --------------------------------------------------------------------------- #
# sync
# --------------------------------------------------------------------------- #
def test_sync_is_a_dry_run_by_default(sut, cli, monkeypatch, capsys):
    _slack(cli, monkeypatch, {"U0SAM": "Sam Tetef", "U01ARUN": "Arun N."})
    assert cli.main(["sync"]) == 0
    out = capsys.readouterr().out
    assert "Dry run" in out
    assert sut.registry.by_id("U0SAM")["display_name"] == "Sam"     # unchanged


def test_sync_apply_normalises_names_to_the_alias(sut, cli, monkeypatch):
    """Names come from the alias, not from Slack. Slack's two name fields
    disagree — the alias is slugified from a real name while display_name took
    the handle — which is how "macon-abernathy" ended up beside "mjabern"."""
    _slack(cli, monkeypatch, {"U0SAM": "Sam Tetef", "U01ARUN": "Arun N."})
    cli.main(["rename", "sam", "--to", "sam-tetef"])
    assert cli.main(["sync", "--apply"]) == 0
    assert sut.registry.by_id("U0SAM")["display_name"] == "Sam Tetef"
    assert sut.registry.by_id("U01ARUN")["display_name"] == "Arun"   # alias is "arun"


def test_sync_does_not_take_a_slack_handle_as_a_name(sut, cli, monkeypatch):
    """The regression: a handle is not a name, and the near-miss between the two
    is what an agent guesses wrong on."""
    _slack(cli, monkeypatch, {"U0SAM": "mjabern", "U01ARUN": "Arun N."})
    cli.main(["sync", "--apply"])
    assert sut.registry.by_id("U0SAM")["display_name"] == "Sam"      # from the alias


def test_sync_reports_alias_drift_without_renaming(sut, cli, monkeypatch, capsys):
    """Renaming is never automatic — an alias is a folder name people rely on."""
    _slack(cli, monkeypatch, {"U0SAM": "Samantha Rivera", "U01ARUN": "Arun N."})
    cli.main(["sync", "--apply"])
    assert "samantha-rivera" in capsys.readouterr().out
    assert sut.registry.by_id("U0SAM")["alias"] == "sam"


def test_sync_survives_slack_being_unreachable(sut, cli, monkeypatch, capsys):
    """Slack is now only consulted for alias drift, so an outage costs that check
    and nothing else — the names still normalise."""
    _slack(cli, monkeypatch, {"U0SAM": "!network is down", "U01ARUN": "Arun N."})
    assert cli.main(["sync", "--apply"]) == 0
    assert "warning" in capsys.readouterr().out
    assert sut.registry.by_id("U0SAM")["display_name"] == "Sam"     # kept, not blanked
    assert sut.registry.by_id("U01ARUN")["display_name"] == "Arun"  # still normalised


def test_sync_says_nothing_to_do_when_names_match(sut, cli, monkeypatch, capsys):
    _slack(cli, monkeypatch, {"U0SAM": "Sam", "U01ARUN": "Arun"})
    cli.main(["sync", "--apply"])            # settle both onto their aliases
    capsys.readouterr()
    assert cli.main(["sync"]) == 0
    assert "up to date" in capsys.readouterr().out


def test_sync_skips_removed_users(sut, cli, monkeypatch):
    cli.main(["remove", "arun", "-y"])
    seen = []

    def _lookup(uid):
        seen.append(uid)
        return "Sam", ""

    monkeypatch.setattr(cli, "_lookup_slack_profile", _lookup)
    cli.main(["sync"])
    assert "U01ARUN" not in seen


# --------------------------------------------------------------------------- #
# requests
# --------------------------------------------------------------------------- #
def test_requests_drops_asks_already_granted(sut, cli, capsys):
    sut.pending.record("access", "U01ARUN")          # already registered
    sut.pending.record("access", "U0NEW")
    assert cli.main(["requests"]) == 0
    out = capsys.readouterr().out
    assert "already done, dropping" in out
    assert "U0NEW" in out
    assert sut.pending.find("access", "U01ARUN") is None


def test_requests_clear_forgets_without_granting(sut, cli, capsys):
    sut.pending.record("access", "U0NEW")
    assert cli.main(["requests", "--clear"]) == 0
    assert "Nobody was granted anything" in capsys.readouterr().out
    assert sut.pending.load() == []
    assert sut.registry.by_id("U0NEW") is None


# --------------------------------------------------------------------------- #
# setup
# --------------------------------------------------------------------------- #
def test_setup_shows_everyone_by_default(sut, cli, capsys):
    assert cli.main(["setup"]) == 0
    out = capsys.readouterr().out
    assert "sam" in out and "arun" in out
    assert "workflow" in out and "calc_root" in out


def test_setup_can_show_one_person(sut, cli, capsys):
    assert cli.main(["setup", "arun"]) == 0
    out = capsys.readouterr().out
    assert "arun" in out and "sam" not in out.split("WHO")[1]


def test_setup_reset_clears_a_decline(sut, cli, capsys):
    sut.setup.decline("U01ARUN", "workflow")
    assert cli.main(["setup", "arun", "--reset", "workflow"]) == 0
    assert sut.setup.state("U01ARUN", "workflow") == "missing"


def test_setup_reset_needs_something_to_reset(sut, cli):
    assert cli.main(["setup", "arun", "--reset", "workflow"]) == 1


def test_setup_reset_needs_a_user(sut, cli, capsys):
    assert cli.main(["setup", "--reset", "workflow"]) == 1
    assert "needs a user" in capsys.readouterr().err


def test_setup_refuses_an_unknown_user(sut, cli):
    assert cli.main(["setup", "nobody"]) == 1


# --------------------------------------------------------------------------- #
# whois, with the newer fields
# --------------------------------------------------------------------------- #
def test_whois_shows_the_root_and_declines(sut, cli, env, capsys):
    root = env.root("arun-calcs")
    cli.main(["set-root", "arun", str(root)])
    sut.setup.decline("U01ARUN", "workflow")
    capsys.readouterr()
    assert cli.main(["whois", "arun"]) == 0
    out = capsys.readouterr().out
    assert str(root) in out
    assert "workflow" in out.split("declined")[1]


def test_whois_says_when_someone_has_no_tree_of_their_own(sut, cli, capsys):
    sut.setup.decline("U01ARUN", "calc_root")
    capsys.readouterr()
    cli.main(["whois", "arun"])
    assert "declined" in capsys.readouterr().out


# --------------------------------------------------------------------------- #
# telemetry switch
# --------------------------------------------------------------------------- #
def test_telemetry_status_reports_what_is_recorded(sut, cli, capsys):
    assert cli.main(["telemetry", "status"]) == 0
    out = capsys.readouterr().out
    assert "metrics" in out and "question text" in out


def test_telemetry_off_then_on(sut, cli):
    assert cli.main(["telemetry", "off"]) == 0
    assert sut.telemetry.effective()["metrics"] is False
    assert cli.main(["telemetry", "on"]) == 0
    assert sut.telemetry.effective()["metrics"] is True


def test_telemetry_content_window_closes_by_itself(sut, cli):
    assert cli.main(["telemetry", "content", "on", "--days", "30"]) == 0
    state = sut.telemetry.effective()
    assert state["content"] is True and state["content_until"]


def test_telemetry_content_rejects_a_past_window(sut, cli):
    assert cli.main(["telemetry", "content", "on", "--until", "2000-01-01"]) == 1


def test_telemetry_content_rejects_a_nonsense_date(sut, cli):
    assert cli.main(["telemetry", "content", "on", "--until", "yesterday"]) == 1


def test_telemetry_exclude_and_include_one_person(sut, cli):
    assert cli.main(["telemetry", "exclude", "arun"]) == 0
    assert "U01ARUN" in sut.telemetry.effective()["excluded_users"]
    assert cli.main(["telemetry", "include", "arun"]) == 0
    assert "U01ARUN" not in sut.telemetry.effective()["excluded_users"]


def test_telemetry_include_of_someone_not_excluded_is_an_error(sut, cli):
    assert cli.main(["telemetry", "include", "arun"]) == 1


def test_unix_user_can_be_recorded_without_re_supplying_a_path(sut, cli, env):
    """Someone on the shared default root has no path to give, and demanding one
    to fill in an account is how the field stayed empty for everybody."""
    root = env.root("arun-calcs")
    cli.main(["set-root", "arun", str(root)])
    assert cli.main(["set-root", "arun", "--unix-user", "aasundi"]) == 0
    entry = sut.registry.resolve("arun")
    assert entry["unix_user"] == "aasundi"
    assert entry["calc_root"] == str(root)        # the root is left alone


def test_unix_user_alone_works_for_someone_on_the_default_root(sut, cli):
    assert cli.main(["set-root", "arun", "--unix-user", "aasundi"]) == 0
    entry = sut.registry.resolve("arun")
    assert entry["unix_user"] == "aasundi" and entry["calc_root"] == ""


def test_set_root_with_neither_a_path_nor_an_account_still_errors(sut, cli):
    assert cli.main(["set-root", "arun"]) != 0
