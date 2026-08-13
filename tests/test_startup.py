"""
Tests for the startup guards in ``aspen/main.py``.

These are security controls that only ever run at boot, which is exactly why they
were the least-covered code in the package: nothing exercised them, so a
regression would surface as a *silently missing* check rather than a failing test.

Two things are being guarded, and both fail closed:

* **Placement.** State that decides who may talk to Aspen, whose files are whose,
  and what Aspen believes about a project must not sit anywhere sandboxed
  analysis code can write. Otherwise generated Python edits the allowlist, or
  edits a note that steers a later turn, and every Python-level check upstream is
  moot.
* **Roots.** Containment *is* the fence in ``roots.resolve``, so a calculations
  root nested inside another is not a boundary at all — one person's fence would
  silently enclose someone else's tree. Unreadable roots are fatal for a
  different reason: they fail on somebody's question instead of at boot, and they
  are what will bite at the service-account cutover.
"""

import os

import pytest


@pytest.fixture
def boot(sut, env, monkeypatch):
    """A registry with two rooted users, ready for the guards to inspect."""
    monkeypatch.setattr(sut, "SANDBOX_WRITE_PATHS", [], raising=False)
    env.register(
        {"slack_user_id": "U0SAM", "alias": "sam", "display_name": "Sam",
         "role": "admin", "calc_root": str(env.root("sam-calcs"))},
        {"slack_user_id": "U01ARUN", "alias": "arun", "display_name": "Arun N.",
         "calc_root": str(env.root("arun-calcs"))},
    )
    return env


# --------------------------------------------------------------------------- #
# Placement
# --------------------------------------------------------------------------- #
def test_a_sane_layout_starts(sut, boot):
    sut._check_state_locations()          # must not raise


@pytest.mark.parametrize("name", [
    "USERS_FILE", "WORKFLOWS_ROOT", "METADATA_ROOT", "METADATA_HISTORY_ROOT",
    "REQUESTS_FILE", "TELEMETRY_DIR", "TELEMETRY_STATE_FILE",
    # The job ledger decides who may cancel what, so a sandbox-writable one is a
    # forgeable cancel. Staging is deliberately NOT here — it holds results and is
    # meant to be readable; its narrower rule is tested below.
    "JOBS_LEDGER",
])
def test_state_inside_the_workspace_is_fatal(sut, boot, monkeypatch, name):
    """Every one of these is writable by generated analysis code if misplaced."""
    monkeypatch.setattr(sut, name, sut.WORKSPACE_ROOT / "leaked")
    with pytest.raises(SystemExit) as exit_info:
        sut._check_state_locations()
    assert "sandbox-writable area" in str(exit_info.value)


def test_state_inside_a_sandbox_write_path_is_fatal(sut, boot, monkeypatch):
    elsewhere = boot.tmp / "sandbox-scratch"
    elsewhere.mkdir()
    monkeypatch.setattr(sut, "SANDBOX_WRITE_PATHS", [str(elsewhere)], raising=False)
    monkeypatch.setattr(sut, "USERS_FILE", elsewhere / "users.json")
    with pytest.raises(SystemExit):
        sut._check_state_locations()


def test_the_state_directory_is_made_private(sut, boot):
    """0700 keeps other accounts on a shared login node out of the registry."""
    sut._check_state_locations()
    assert (os.stat(sut.STATE_DIR).st_mode & 0o777) == 0o700


# --------------------------------------------------------------------------- #
# Roots
# --------------------------------------------------------------------------- #
def test_sane_roots_start(sut, boot):
    sut._check_calculations_roots()       # must not raise


def test_a_nested_root_refuses_to_start(sut, boot):
    """Containment is the fence, so a root inside a root bounds nothing."""
    nested = boot.calcs / "sam-calcs" / "inner"
    nested.mkdir()
    sut.registry.save([
        dict(u, calc_root=str(nested)) if u["alias"] == "arun" else u
        for u in sut.registry.users()
    ])
    with pytest.raises(SystemExit) as exit_info:
        sut._check_calculations_roots()
    message = str(exit_info.value)
    assert "nested" in message
    assert "set-root" in message           # it says how to fix it


def test_a_missing_root_refuses_to_start(sut, boot):
    import shutil
    shutil.rmtree(boot.calcs / "arun-calcs")
    with pytest.raises(SystemExit) as exit_info:
        sut._check_calculations_roots()
    assert "@arun" in str(exit_info.value)


def test_an_unreadable_root_refuses_to_start(sut, boot):
    """The check that will bite at the service-account cutover, as intended."""
    blocked = boot.calcs / "arun-calcs"
    original = os.stat(blocked).st_mode
    os.chmod(blocked, 0o000)
    try:
        if os.access(blocked, os.R_OK):
            pytest.skip("running as a user that ignores mode bits (root?)")
        with pytest.raises(SystemExit) as exit_info:
            sut._check_calculations_roots()
        assert "readable" in str(exit_info.value)
    finally:
        os.chmod(blocked, original)


def test_a_shared_root_is_checked_too(sut, boot, monkeypatch):
    monkeypatch.setattr(sut, "SHARED_CALC_ROOTS", {"smb": str(boot.tmp / "gone")})
    with pytest.raises(SystemExit) as exit_info:
        sut._check_calculations_roots()
    assert "@smb" in str(exit_info.value)


def test_everyone_sharing_the_default_root_is_not_nesting(sut, boot):
    """The single-root deployment must not look like a misconfiguration."""
    sut.registry.save([dict(u, calc_root="") for u in sut.registry.users()])
    sut._check_calculations_roots()       # must not raise


def test_the_guard_runs_as_part_of_the_startup_check(sut, boot):
    """Wiring: the roots check is reached from _check_state_locations, not
    something a caller has to remember to invoke."""
    nested = boot.calcs / "sam-calcs" / "inner"
    nested.mkdir()
    sut.registry.save([
        dict(u, calc_root=str(nested)) if u["alias"] == "arun" else u
        for u in sut.registry.users()
    ])
    with pytest.raises(SystemExit):
        sut._check_state_locations()


# --------------------------------------------------------------------------- #
# Job staging vs the calculations roots
#
# Containment is the fence on both sides, so an overlap in either direction
# breaks something: staging inside a root makes every staged copy a write into
# someone's tree (the invariant Aspen holds absolutely), and a root inside
# staging puts a directory the agent can write behind roots.resolve's fence.
# --------------------------------------------------------------------------- #
def test_sane_staging_starts(sut, boot, monkeypatch):
    monkeypatch.setattr(sut, "JOBS_SUBMIT_ENABLED", True)
    sut.main._check_jobs_staging()        # must not raise


def test_staging_inside_a_calculations_root_is_fatal(sut, boot, monkeypatch):
    monkeypatch.setattr(sut, "JOBS_SUBMIT_ENABLED", True)
    monkeypatch.setattr(sut, "JOBS_STAGING_ROOT", boot.calcs / "sam-calcs" / "aspen-jobs")
    with pytest.raises(SystemExit) as exit_info:
        sut.main._check_jobs_staging()
    assert "inside the calculations root" in str(exit_info.value)


def test_a_calculations_root_inside_staging_is_fatal(sut, boot, monkeypatch):
    monkeypatch.setattr(sut, "JOBS_SUBMIT_ENABLED", True)
    monkeypatch.setattr(sut, "JOBS_STAGING_ROOT", boot.calcs)
    with pytest.raises(SystemExit) as exit_info:
        sut.main._check_jobs_staging()
    assert "inside job staging" in str(exit_info.value)


def test_staging_is_not_checked_when_submission_is_off(sut, boot, monkeypatch):
    """A deployment without submission should not be blocked by its staging config."""
    monkeypatch.setattr(sut, "JOBS_SUBMIT_ENABLED", False)
    monkeypatch.setattr(sut, "JOBS_STAGING_ROOT", boot.calcs / "sam-calcs" / "nested")
    sut.main._check_jobs_staging()        # must not raise


def test_the_staging_guard_runs_as_part_of_the_startup_check(sut, boot, monkeypatch):
    """A guard nothing calls is a guard that does not exist."""
    monkeypatch.setattr(sut, "JOBS_SUBMIT_ENABLED", True)
    monkeypatch.setattr(sut, "JOBS_STAGING_ROOT", boot.calcs / "sam-calcs" / "aspen-jobs")
    with pytest.raises(SystemExit):
        sut._check_state_locations()


def test_staging_is_readable_so_results_can_be_collected(sut, boot, monkeypatch):
    """Deliberately NOT private, unlike everything else this guard covers.

    Staging holds a run's outputs — the ORCA .out, the optimised geometry — which
    are the point of submitting it. The first real submission left them where only
    the bot's account could read, so neither the user nor Aspen could get them back.
    It lives under WORKSPACE_ROOT and is world-readable for that reason.
    """
    monkeypatch.setattr(sut, "JOBS_SUBMIT_ENABLED", True)
    monkeypatch.setattr(sut, "JOBS_STAGING_ROOT", sut.WORKSPACE_ROOT / "jobs")
    sut._check_state_locations()          # must NOT raise, though it is in the workspace
    assert sut.JOBS_STAGING_ROOT.stat().st_mode & 0o005


@pytest.mark.parametrize("area", ["figures", "cache"])
def test_staging_inside_the_jails_writable_binds_is_fatal(sut, boot, monkeypatch, area):
    """The narrower rule that replaced "staging must be outside the workspace".

    What matters is that no agent-writable surface reaches staging — otherwise
    generated analysis code plants a script and Aspen submits it. The jail
    bind-mounts only figures/ and cache/ read-write, so those are what to refuse.
    """
    monkeypatch.setattr(sut, "JOBS_SUBMIT_ENABLED", True)
    monkeypatch.setattr(sut, "JOBS_STAGING_ROOT", sut.WORKSPACE_ROOT / area / "jobs")
    with pytest.raises(SystemExit) as exit_info:
        sut._check_state_locations()
    assert "the agent can write" in str(exit_info.value)


def test_staging_inside_a_sandbox_write_path_is_fatal(sut, boot, monkeypatch):
    monkeypatch.setattr(sut, "JOBS_SUBMIT_ENABLED", True)
    scratch = boot.tmp / "sandbox-scratch"
    scratch.mkdir()
    monkeypatch.setattr(sut, "SANDBOX_WRITE_PATHS", [str(scratch)], raising=False)
    monkeypatch.setattr(sut, "JOBS_STAGING_ROOT", scratch / "jobs")
    with pytest.raises(SystemExit):
        sut._check_state_locations()
