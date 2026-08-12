"""
Tests for the Slurm job surface (spec §19) — staging, the ledger, and the argv.

Cancellation has its own file (``test_jobs_cancel.py``), because the ownership
boundary is the part that carries the weight and deserves to be read on its own.

The properties asserted here:

* **The model never authors a job.** No tool accepts a script path or a script
  body, and ``template_mode`` is a closed enum — an unknown mode is refused, not
  forwarded.
* **Nothing is written inside a calculations root.** Structures are copied out,
  never linked, and the source tree is untouched.
* **The ledger precedes the scheduler.** A submission whose ledger write failed is
  never run.
* **No secret reaches a compute node.** ``sbatch`` defaults to ``--export=ALL``,
  so the environment handed to the orchestrator is the boundary.
* **The caps are Python, not prose.**
"""

import json

import pytest


@pytest.fixture
def jobs_env(sut, env):
    """One user with a root holding two structures, submission enabled."""
    root = env.root("sam-calcs")
    env.register(
        {"slack_user_id": "U0SAM", "alias": "sam", "display_name": "Sam",
         "role": "admin", "calc_root": str(root)},
        {"slack_user_id": "U01ARUN", "alias": "arun", "display_name": "Arun N.",
         "calc_root": str(env.root("arun-calcs"))},
    )
    structures = root / "thermolysin" / "structures"
    structures.mkdir(parents=True)
    for name in ("a.xyz", "b.xyz"):
        (structures / name).write_text("1\n\nH 0.0 0.0 0.0\n")
    return {"root": root, "structures": structures, "env": env}


class FakeProc:
    """A stand-in for ``subprocess.run``'s result."""

    def __init__(self, stdout="", stderr="", returncode=0):
        self.stdout, self.stderr, self.returncode = stdout, stderr, returncode


# --------------------------------------------------------------------------- #
# The model never authors a job
# --------------------------------------------------------------------------- #
def test_no_job_tool_accepts_a_script_path_or_body(sut):
    """The load-bearing difference from the original design.

    §18.2 said only that a script path must resolve inside a staging directory.
    That is necessary but not sufficient: a tool that accepts a path the model
    chose is one writable staging directory away from "the model wrote the job
    body". So the schema must not offer the concept at all.
    """
    for spec in sut.TOOL_SPECS:
        if spec["name"] not in ("submit_orca_batch", "cancel_orca_batch", "list_my_jobs"):
            continue
        props = set(spec["input_schema"].get("properties", {}))
        for forbidden in ("script", "script_path", "code", "command", "sbatch_args",
                          "extra_args", "wrap", "argv", "job_script"):
            assert forbidden not in props, (
                f"{spec['name']} must not accept {forbidden!r}: a job runs unjailed "
                "on a compute node, so its body must never come from the model"
            )


def test_neither_write_tool_takes_an_owner(sut):
    """C9 applied to compute: the requester comes from the Slack event only.

    A model can be argued into passing any argument it is handed; it cannot pass a
    value it never receives. If someone adds an ``owner`` here to be helpful, this
    test is what should stop them.
    """
    for name in ("submit_orca_batch", "cancel_orca_batch", "list_my_jobs"):
        spec = next(s for s in sut.TOOL_SPECS if s["name"] == name)
        props = spec["input_schema"].get("properties", {})
        if name == "submit_orca_batch":
            # `owner` here names whose *calculations* to read the structures from —
            # a read, fenced like every other read. It never selects who the job
            # belongs to; that is always the speaker.
            assert set(props) <= {"path", "owner", "template_mode", "confirm_token"}
        else:
            assert "owner" not in props
            assert "user" not in props and "slack_user_id" not in props


def test_template_mode_is_a_closed_enum(sut, jobs_env):
    """An unknown mode is refused, never forwarded to the command line."""
    for bad in ("--wrap", "; rm -rf /", "ca-fixed; sbatch evil.sh", "unknown-mode"):
        with pytest.raises(sut.jobs.JobsError) as exc:
            sut.staging.mode_flag(bad)
        assert "Unknown template mode" in str(exc.value)

    # Every advertised mode maps to a flag the pipeline actually has.
    for mode in sut.staging.TEMPLATE_MODES:
        flag = sut.staging.mode_flag(mode)
        assert flag == "" or flag.startswith("--")


def test_submit_argv_contains_only_derived_values(sut, jobs_env, tmp_path):
    """The whole argv is inspectable, and nothing in it came from a conversation."""
    argv = sut.jobs.build_submit_argv(
        staging_dir=tmp_path / "stage", out_dir=tmp_path / "out",
        template_mode="quick", dry_run=True,
    )
    assert argv[0] == sut.JOBS_PIPELINE_BIN
    assert "--no-submit" in argv
    assert "--quick" in argv
    assert not any("wrap" in a for a in argv)


def test_the_enum_matches_the_tool_schema(sut):
    """The schema's enum and the implementation's map cannot drift apart."""
    spec = next(s for s in sut.TOOL_SPECS if s["name"] == "submit_orca_batch")
    assert (set(spec["input_schema"]["properties"]["template_mode"]["enum"])
            == set(sut.staging.TEMPLATE_MODES))


# --------------------------------------------------------------------------- #
# Staging writes nothing into a calculations root
# --------------------------------------------------------------------------- #
def test_staging_copies_and_leaves_the_source_untouched(sut, jobs_env):
    files, scope = sut.staging.collect_structures("thermolysin/structures", "", "U0SAM")
    assert [f.name for f in files] == ["a.xyz", "b.xyz"]

    before = {p.name: p.read_bytes() for p in jobs_env["structures"].iterdir()}
    dest = sut.staging.stage(
        requester_uid="U0SAM", thread_ts="1723480000.1", structures=files,
        scope=scope, template_mode="ca-fixed", source_rel="thermolysin/structures",
    )
    after = {p.name: p.read_bytes() for p in jobs_env["structures"].iterdir()}
    assert before == after, "staging must not modify the source tree"

    # Copies, not symlinks: a symlink would let a writer in staging reach back
    # through it into the calculations root.
    for name in ("a.xyz", "b.xyz"):
        staged = dest / name
        assert staged.is_file() and not staged.is_symlink()
    assert (dest / "provenance.json").is_file()


def test_staging_records_provenance(sut, jobs_env):
    files, scope = sut.staging.collect_structures("thermolysin/structures", "", "U0SAM")
    dest = sut.staging.stage(
        requester_uid="U0SAM", thread_ts="1723480000.1", structures=files,
        scope=scope, template_mode="quick", source_rel="thermolysin/structures",
    )
    prov = json.loads((dest / "provenance.json").read_text())
    assert prov["requested_by"] == "U0SAM"
    assert prov["template_mode"] == "quick"
    assert {s["name"] for s in prov["structures"]} == {"a.xyz", "b.xyz"}
    assert all(len(s["sha256"]) == 64 for s in prov["structures"])


def test_staging_lands_under_the_requesters_own_directory(sut, jobs_env):
    """The destination is derived from the registry and the event, not from input."""
    dest = sut.jobs.staging_dir_for("U0SAM", "1723480000.123456")
    assert dest.parent.name == "sam__U0SAM"
    assert sut.jobs._within(dest, sut.jobs.user_staging_root("U0SAM"))
    assert not sut.jobs._within(dest, sut.jobs.user_staging_root("U01ARUN"))


def test_a_rename_does_not_orphan_a_users_staging(sut, env):
    """Found by ID, like workflows and metadata — the alias is only a label."""
    env.register({"slack_user_id": "U0SAM", "alias": "sam", "display_name": "Sam"})
    before = sut.jobs.user_staging_root("U0SAM")
    env.register({"slack_user_id": "U0SAM", "alias": "sam-w", "display_name": "Sam"})
    after = sut.jobs.user_staging_root("U0SAM")
    assert before.name == "sam__U0SAM" and after.name == "sam-w__U0SAM"
    # Both still resolve to the same user, and neither can be another user's.
    assert before.parent == after.parent


def test_staging_refuses_paths_outside_the_root(sut, jobs_env):
    """The ordinary root fence, reached through roots.resolve like every other tool."""
    for bad in ("../../etc", "/etc", "thermolysin/../../../etc"):
        with pytest.raises(sut.jobs.JobsError):
            sut.staging.collect_structures(bad, "", "U0SAM")


def test_staging_refuses_a_directory_with_no_structures(sut, jobs_env):
    (jobs_env["root"] / "empty").mkdir()
    with pytest.raises(sut.jobs.JobsError) as exc:
        sut.staging.collect_structures("empty", "", "U0SAM")
    assert "No .xyz" in str(exc.value)


def test_resubmitting_in_one_thread_does_not_mix_two_batches(sut, jobs_env):
    files, scope = sut.staging.collect_structures("thermolysin/structures", "", "U0SAM")
    first = sut.staging.stage(
        requester_uid="U0SAM", thread_ts="1723480000.1", structures=files,
        scope=scope, template_mode="ca-fixed", source_rel="thermolysin/structures")
    second = sut.staging.stage(
        requester_uid="U0SAM", thread_ts="1723480000.1", structures=files,
        scope=scope, template_mode="quick", source_rel="thermolysin/structures")
    assert first != second


# --------------------------------------------------------------------------- #
# No secret reaches a compute node
# --------------------------------------------------------------------------- #
def test_submit_env_carries_no_secrets(sut, monkeypatch):
    """``sbatch`` defaults to --export=ALL, so this environment is the boundary."""
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-real-secret")
    monkeypatch.setenv("AGENT_INTERNAL_SECRET", "deadbeef")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-real")
    monkeypatch.setenv("SOME_OTHER_TOKEN", "nope")
    monkeypatch.setenv("MY_PASSWORD", "nope")
    monkeypatch.setenv("PATH", "/usr/bin:/bin")

    env = sut.jobs.submit_env()

    for name in ("SLACK_BOT_TOKEN", "SLACK_APP_TOKEN", "ANTHROPIC_API_KEY",
                 "AGENT_INTERNAL_SECRET", "SOME_OTHER_TOKEN", "MY_PASSWORD"):
        assert name not in env, f"{name} must never reach a compute node"
    for value in ("xoxb-real-secret", "deadbeef", "sk-ant-real"):
        assert value not in "".join(env.values())
    # ...while the pipeline still gets what it needs to run.
    assert env["PATH"] == "/usr/bin:/bin"


def test_submit_env_passthrough_cannot_smuggle_a_secret(sut, monkeypatch):
    """An operator listing a secret in the passthrough does not get one exported."""
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-real-secret")
    monkeypatch.setattr(sut, "JOBS_ENV_PASSTHROUGH", ["SLACK_BOT_TOKEN"])
    assert "SLACK_BOT_TOKEN" not in sut.jobs.submit_env()


def test_submit_env_names_the_exported_set(sut):
    """SBATCH_EXPORT is set explicitly, so a future sbatch default cannot widen it."""
    env = sut.jobs.submit_env()
    assert env.get("SBATCH_EXPORT")
    for name in ("SLACK_BOT_TOKEN", "AGENT_INTERNAL_SECRET"):
        assert name not in env["SBATCH_EXPORT"]


def test_export_none_is_not_the_default(sut):
    """Deliberate: --export=NONE silently no-ops the pipeline's auto-rerun.

    The ORCA job script finds ``xas-rerun-orca`` on an inherited PATH behind a
    ``command -v`` guard, so NONE turns failure triage off with no error and no log
    line. Scrubbing the parent environment gets the security result instead. If
    someone changes this default, they should have to change this test and read why.
    """
    exported = sut.jobs.submit_env()["SBATCH_EXPORT"]
    assert exported != "NONE"
    assert "PATH" in exported


# --------------------------------------------------------------------------- #
# The ledger precedes the scheduler
# --------------------------------------------------------------------------- #
def test_dry_run_submits_nothing_and_issues_a_single_use_token(sut, jobs_env, monkeypatch):
    calls = []

    def fake_run(argv, **kw):
        calls.append(argv)
        return FakeProc(stdout="would submit 2\n")

    monkeypatch.setattr(sut.jobs.subprocess, "run", fake_run)
    result = sut.jobs.dry_run(
        requester_uid="U0SAM", thread_ts="1723480000.1",
        rel="thermolysin/structures", owner="", template_mode="ca-fixed",
    )

    assert len(calls) == 1 and "--no-submit" in calls[0]
    assert result["token"]
    # Nothing recorded: a dry run is not a submission.
    assert sut.jobs.batches_for("U0SAM") == []

    # The token is single-use.
    payload = sut.jobs.redeem_token(result["token"], "submit", "U0SAM", "1723480000.1")
    assert payload["template_mode"] == "ca-fixed"
    with pytest.raises(sut.jobs.JobsError):
        sut.jobs.redeem_token(result["token"], "submit", "U0SAM", "1723480000.1")


def test_a_token_cannot_be_replayed_by_another_user_or_thread(sut, jobs_env):
    token = sut.jobs.issue_token("submit", "U0SAM", "1723480000.1", {"x": 1})
    with pytest.raises(sut.jobs.JobsError):
        sut.jobs.redeem_token(token, "submit", "U01ARUN", "1723480000.1")
    with pytest.raises(sut.jobs.JobsError):
        sut.jobs.redeem_token(token, "submit", "U0SAM", "9999999999.9")
    # ...and a token for one action cannot be spent on the other.
    cancel_token = sut.jobs.issue_token("cancel", "U0SAM", "1723480000.1", {})
    with pytest.raises(sut.jobs.JobsError):
        sut.jobs.redeem_token(cancel_token, "submit", "U0SAM", "1723480000.1")


def test_commit_records_the_batch_before_running_the_pipeline(sut, jobs_env, monkeypatch):
    """Ordering, asserted by making the pipeline observe the ledger mid-flight."""
    seen = {}

    def fake_run(argv, **kw):
        if "--no-submit" in argv:
            return FakeProc(stdout="dry ok")
        seen["batches_at_submit_time"] = len(sut.jobs.batches_for("U0SAM"))
        return FakeProc(stdout="Submitted batch job 4242\n")

    monkeypatch.setattr(sut.jobs.subprocess, "run", fake_run)
    preview = sut.jobs.dry_run(
        requester_uid="U0SAM", thread_ts="1723480000.1",
        rel="thermolysin/structures", owner="", template_mode="ca-fixed",
    )
    result = sut.jobs.commit(
        requester_uid="U0SAM", thread_ts="1723480000.1", token=preview["token"],
    )
    assert seen["batches_at_submit_time"] == 1, (
        "the ledger row must exist BEFORE the pipeline is invoked — a job with no "
        "row is one nobody can cancel through Aspen or attribute afterwards"
    )
    assert result["recorded"] == 1
    assert sut.jobs.jobs_for_batch(result["batch_id"])[0]["job_id"] == "4242"


def test_a_failed_ledger_write_aborts_the_submission(sut, jobs_env, monkeypatch):
    ran = []

    def fake_run(argv, **kw):
        ran.append(argv)
        return FakeProc(stdout="dry ok")

    monkeypatch.setattr(sut.jobs.subprocess, "run", fake_run)
    preview = sut.jobs.dry_run(
        requester_uid="U0SAM", thread_ts="1723480000.1",
        rel="thermolysin/structures", owner="", template_mode="ca-fixed",
    )
    ran.clear()

    def boom(**kw):
        raise OSError("disk full")

    monkeypatch.setattr(sut.jobs, "record_batch", boom)
    with pytest.raises(OSError):
        sut.jobs.commit(requester_uid="U0SAM", thread_ts="1723480000.1",
                        token=preview["token"])
    assert ran == [], "nothing may be submitted if the ledger write failed"


def test_the_ledger_and_staging_live_outside_the_workspace(sut, env):
    """They are authorization inputs, so the §7 rule applies: STATE_DIR steers."""
    assert not sut.jobs._within(sut.JOBS_LEDGER, sut.WORKSPACE_ROOT)
    assert not sut.jobs._within(sut.JOBS_STAGING_ROOT, sut.WORKSPACE_ROOT)


# --------------------------------------------------------------------------- #
# Caps are Python, not prose
# --------------------------------------------------------------------------- #
def test_per_submission_structure_cap(sut, jobs_env, monkeypatch):
    monkeypatch.setattr(sut, "JOBS_MAX_STRUCTURES", 1)
    with pytest.raises(sut.jobs.JobsError) as exc:
        sut.jobs.check_caps("U0SAM", 2)
    assert "per-submission cap" in str(exc.value)


def test_daily_submission_cap(sut, jobs_env, monkeypatch):
    monkeypatch.setattr(sut, "JOBS_MAX_SUBMITS_PER_DAY", 1)
    sut.jobs.record_batch(
        slack_user_id="U0SAM", alias="sam", thread_ts="1", project="p",
        owner_scope="sam", template_mode="ca-fixed",
        staging_dir=sut.JOBS_STAGING_ROOT / "x", structures=1, argv=["x"],
    )
    with pytest.raises(sut.jobs.JobsError) as exc:
        sut.jobs.check_caps("U0SAM", 1)
    assert "daily limit" in str(exc.value)


def test_submission_is_refused_when_the_feature_is_off(sut, jobs_env, monkeypatch):
    monkeypatch.setattr(sut, "JOBS_SUBMIT_ENABLED", False)
    with pytest.raises(sut.jobs.JobsError) as exc:
        sut.jobs.dry_run(requester_uid="U0SAM", thread_ts="1",
                         rel="thermolysin/structures", owner="", template_mode="ca-fixed")
    assert "switched off" in str(exc.value)


def test_a_non_registered_user_cannot_submit(sut, jobs_env):
    """The demo case: a visitor is not in the registry and must not spend compute."""
    with pytest.raises(sut.jobs.JobsError) as exc:
        sut.jobs.dry_run(requester_uid="UVISITOR", thread_ts="1",
                         rel="thermolysin/structures", owner="", template_mode="ca-fixed")
    assert "registered Aspen users" in str(exc.value)


def test_a_removed_user_cannot_submit(sut, env):
    env.register({"slack_user_id": "U0GONE", "alias": "gone", "display_name": "Gone",
                  "status": "removed"})
    with pytest.raises(sut.jobs.JobsError):
        sut.jobs.require_registered("U0GONE")


# --------------------------------------------------------------------------- #
# Reporting a pipeline failure
#
# Shaped by what the real pipeline actually does, which was not guessable from
# its code: the *reason* goes to stderr and the *summary* to stdout, and stderr
# also carries a Python traceback.
# --------------------------------------------------------------------------- #
def test_pipeline_errors_combine_both_streams(sut):
    proc = FakeProc(
        returncode=1,
        stdout="ERROR: 2 of 2 XYZ file(s) failed\n  - /stage/a.xyz\n  - /stage/b.xyz\n",
        stderr=('Traceback (most recent call last):\n'
                '  File "/x/orchestrate.py", line 5, in main\n'
                '    raise SystemExit\n'
                '  ERROR: Missing charge and/or multiplicity in XYZ header of a.xyz.\n'),
    )
    text = sut.jobs._pipeline_error_text(proc)
    assert "Missing charge" in text, "the cause (stderr) must survive"
    assert "2 of 2 XYZ file(s) failed" in text, "the summary (stdout) must survive"
    assert "Traceback" not in text and "orchestrate.py" not in text


def test_pipeline_errors_are_capped(sut):
    proc = FakeProc(returncode=1,
                    stdout="\n".join(f"ERROR: structure {i} is bad" for i in range(400)))
    text = sut.jobs._pipeline_error_text(proc)
    assert len(text) < 2000, "a 400-structure failure is not a Slack message"
    assert "truncated" in text


def test_an_unrecognisable_failure_still_says_something(sut):
    assert "exit 3" in sut.jobs._pipeline_error_text(FakeProc(returncode=3))


def test_a_rejected_dry_run_leaves_no_staging_behind(sut, jobs_env, monkeypatch):
    """Otherwise a user iterating on bad inputs accumulates a copy per attempt."""
    monkeypatch.setattr(sut.jobs.subprocess, "run",
                        lambda argv, **kw: FakeProc(returncode=1, stdout="ERROR: nope"))
    with pytest.raises(sut.jobs.JobsError):
        sut.jobs.dry_run(requester_uid="U0SAM", thread_ts="1723480000.1",
                         rel="thermolysin/structures", owner="", template_mode="ca-fixed")
    user_root = sut.jobs.user_staging_root("U0SAM")
    leftovers = list(user_root.glob("*")) if user_root.exists() else []
    assert leftovers == [], f"a failed dry run left {leftovers} behind"


def test_discard_staging_refuses_paths_outside_the_staging_root(sut, jobs_env, tmp_path):
    """A cleanup helper that can be pointed anywhere is a delete primitive."""
    victim = tmp_path / "not-staging"
    victim.mkdir()
    (victim / "important.txt").write_text("hello")
    sut.jobs._discard_staging(victim)
    assert (victim / "important.txt").exists(), "cleanup must never leave the staging root"
