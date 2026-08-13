"""
Tests for reading a submitted job's output back (``aspen/results.py``).

A batch writes into Aspen's staging area rather than into anybody's calculations
tree — that is the invariant staging exists to protect — and until now that meant
the outputs, which are the entire point of the run, were the one thing Aspen could
not open. The properties that make closing the gap safe, each pinned here:

* **The model names a batch, never a path.** The directory comes from the ledger
  row Aspen wrote at submit time; nothing in a conversation can point the reader
  somewhere else.
* **The fence holds after symlinks.** A job runs unjailed on a compute node, so a
  run could leave a symlink in its own output; following it out is refused.
* **It stays read-only.** There is no copy-in, no write, and a staged geometry
  cannot be handed to a submission — the user gets a `cp` line and runs it as
  themselves, which keeps "Aspen writes nothing inside a calculations root" true.
* **A demo visitor sees none of it.** Job results are real users' work.
"""

import pytest


@pytest.fixture
def batch(sut, env):
    """One registered user and one finished batch with output on disk."""
    root = env.root("sam-calcs")
    env.register(
        {"slack_user_id": "U0SAM", "alias": "sam", "display_name": "Sam",
         "role": "admin", "calc_root": str(root)},
        {"slack_user_id": "U01ARUN", "alias": "arun", "display_name": "Arun N.",
         "calc_root": str(env.root("arun-calcs"))},
    )
    staging = sut.jobs.staging_dir_for("U0SAM", "1723480000.1")
    (staging / "orca").mkdir(parents=True)
    (staging / "smoketest-h2o.out").write_text(
        "ORCA TERMINATED NORMALLY\nTHE OPTIMIZATION HAS CONVERGED\n")
    (staging / "smoketest-h2o.xyz").write_text("1\n\nH 0.0 0.0 0.0\n")
    (staging / "orca" / "run.log").write_text("ORCA TERMINATED NORMALLY\n")
    batch_id = sut.jobs.record_batch(
        slack_user_id="U0SAM", alias="sam", thread_ts="1723480000.1",
        project="thermolysin", owner_scope="sam", template_mode="ca-fixed",
        staging_dir=staging, structures=1, argv=["xas-run-batch"])
    return {"id": batch_id, "dir": staging, "root": root, "env": env}


# --------------------------------------------------------------------------- #
# The fence
# --------------------------------------------------------------------------- #
def test_a_batch_id_resolves_inside_its_own_results(sut, batch):
    path, scope, error = sut.results.resolve(batch["id"], "smoketest-h2o.out", "U0SAM")
    assert error == ""
    assert path == batch["dir"] / "smoketest-h2o.out"
    assert scope["kind"] == "results" and scope["batch_id"] == batch["id"]


def test_the_display_form_says_which_run_it_came_from(sut, batch):
    path, scope, _ = sut.results.resolve(batch["id"], "orca/run.log", "U0SAM")
    assert sut.results.relative_to_scope(path, scope) == f"batch:{batch['id']}/orca/run.log"


def test_dot_dot_cannot_climb_out(sut, batch):
    _p, _s, error = sut.results.resolve(batch["id"], "../../../etc/passwd", "U0SAM")
    assert "outside" in error


def test_an_absolute_path_does_not_replace_the_base(sut, batch):
    """pathlib's '/' happily discards the left side for an absolute right side —
    the containment check is what catches it, so pin that it does."""
    _p, _s, error = sut.results.resolve(batch["id"], "/etc/passwd", "U0SAM")
    assert "outside" in error


def test_a_symlink_out_of_the_batch_is_refused(sut, batch, tmp_path):
    """A job runs unjailed, so its output directory is not trusted to contain only
    files — containment is checked after the link is followed."""
    secret = tmp_path / "elsewhere.txt"
    secret.write_text("not yours")
    (batch["dir"] / "escape.txt").symlink_to(secret)
    _p, _s, error = sut.results.resolve(batch["id"], "escape.txt", "U0SAM")
    assert "outside" in error


def test_an_unknown_batch_says_where_ids_come_from(sut, batch):
    _p, _s, error = sut.results.resolve("0123456789abcdef", ".", "U0SAM")
    assert "no batch" in error and "list_my_jobs" in error


def test_a_path_shaped_batch_id_never_reaches_a_lookup(sut, batch, monkeypatch):
    """It is refused for being malformed, before anything treats it as a key."""
    called = []
    monkeypatch.setattr(sut.jobs, "batch_by_id", lambda bid: called.append(bid))
    for bad in ("../../etc", "/etc/passwd", "batch id", "", "SELECT *", "abc"):
        _p, _s, error = sut.results.resolve(bad, ".", "U0SAM")
        assert error.startswith("Error"), bad
    assert called == [], "a malformed ID is not a ledger question"


def test_a_batch_outside_the_staging_root_is_refused(sut, batch, tmp_path, monkeypatch):
    """Reading is confined to one tree, with no case analysis about what else a
    recorded path might have been."""
    stray = tmp_path / "somewhere-else"
    stray.mkdir()
    (stray / "secrets.txt").write_text("x")
    row = dict(sut.jobs.batch_by_id(batch["id"]), staging_dir=str(stray))
    monkeypatch.setattr(sut.jobs, "batch_by_id", lambda bid: row)
    _p, _s, error = sut.results.resolve(batch["id"], "secrets.txt", "U0SAM")
    assert "staging area" in error


def test_a_batch_the_sandbox_could_write_is_refused(sut, batch, monkeypatch):
    """Anything the jail can write is content the model could have planted, which
    is the one thing this reader must never serve."""
    monkeypatch.setattr(sut, "SANDBOX_WRITE_PATHS", [str(sut.JOBS_STAGING_ROOT)])
    _p, _s, error = sut.results.resolve(batch["id"], ".", "U0SAM")
    assert "sandbox" in error


def test_a_cleaned_up_batch_says_so(sut, batch):
    import shutil
    shutil.rmtree(batch["dir"])
    _p, _s, error = sut.results.resolve(batch["id"], ".", "U0SAM")
    assert "no results directory" in error


def test_a_colleague_may_read_a_batch_they_did_not_submit(sut, batch):
    """Reads are flat, exactly as they are for calculations roots — they are flat
    on the shared filesystem already, and staging is deliberately world-readable."""
    path, _scope, error = sut.results.resolve(batch["id"], "smoketest-h2o.out", "U01ARUN")
    assert error == "" and path.is_file()


def test_a_demo_visitor_cannot_reach_job_results(sut, batch, monkeypatch):
    monkeypatch.setattr(sut, "DEMO_ENABLED", True)
    sut.demo.clear()
    session, refusal = sut.demo.start("U0STRANGER", "1700000000.000100")
    assert session is not None, refusal
    try:
        _p, _s, error = sut.results.resolve(batch["id"], ".", session.user_id)
        assert "demo session" in error
    finally:
        sut.demo.clear()


# --------------------------------------------------------------------------- #
# Through the tools
# --------------------------------------------------------------------------- #
def test_read_file_opens_a_finished_run(sut, batch):
    out = sut._read_file("smoketest-h2o.out", "", "U0SAM", batch["id"])
    assert "ORCA TERMINATED NORMALLY" in out
    assert f"batch:{batch['id']}/smoketest-h2o.out" in out


def test_list_directory_shows_what_a_run_produced(sut, batch):
    out = sut._list_directory(".", "", "U0SAM", batch["id"])
    assert "smoketest-h2o.out" in out and "[dir] orca" in out
    assert "metadata" not in out, (
        "a batch's results are not a project; there are no notes to offer"
    )


def test_attach_file_can_hand_over_a_run_output(sut, batch):
    text, paths = sut._attach_file("smoketest-h2o.out", "", "U0SAM", batch["id"])
    assert paths == [str(batch["dir"] / "smoketest-h2o.out")]
    assert f"batch:{batch['id']}" in text


def test_check_orca_run_descends_into_the_batch(sut, batch):
    """The pipeline lays the tree out, not the user, so the outputs are below."""
    text, _ = sut.tools._check_orca_run({"path": ".", "batch_id": batch["id"]},
                                        {"user_id": "U0SAM"})
    assert "CONVERGED" in text
    assert "orca/run.log" in text, "one level down is where a real run puts it"


def test_check_orca_run_hands_over_the_copy_command(sut, batch):
    """A staged geometry cannot be fed to submit_calculation, so say what can."""
    text, _ = sut.tools._check_orca_run({"path": ".", "batch_id": batch["id"]},
                                        {"user_id": "U0SAM"})
    assert "cp -r" in text and str(batch["dir"]) in text
    assert str(batch["root"]) in text, "copied into the reader's own tree"


def test_naming_both_fences_at_once_is_an_error(sut, batch):
    """The same rule as '@alias/path' plus owner=: the ambiguity is the model's to
    resolve, and quietly picking one is how a read lands somewhere nobody chose."""
    for kwargs in ({"owner": "arun"}, {}):
        rel = "@arun/x" if not kwargs else "x"
        _p, _s, error = sut.tools._scoped(rel, kwargs.get("owner", ""), "U0SAM",
                                          batch["id"])
        assert "not both" in error


def test_no_batch_id_still_reads_calculations(sut, batch):
    """The default path must be untouched by any of this."""
    (batch["root"] / "notes.txt").write_text("hello")
    assert "hello" in sut._read_file("notes.txt", "", "U0SAM")


# --------------------------------------------------------------------------- #
# What is advertised
# --------------------------------------------------------------------------- #
def test_the_read_tools_offer_the_fence_when_jobs_are_on(sut, monkeypatch):
    monkeypatch.setattr(sut, "JOBS_SUBMIT_ENABLED", True)
    specs = {s["name"]: s for s in sut.tools.active_specs(True)}
    for name in ("read_file", "list_directory", "attach_file", "check_orca_run"):
        assert "batch_id" in specs[name]["input_schema"]["properties"], name


def test_no_jobs_means_no_batch_id_is_offered(sut, monkeypatch):
    """Withheld by omission, like the job tools themselves: where there is no
    submission there are no batches, so the question is never put to the model."""
    monkeypatch.setattr(sut, "JOBS_SUBMIT_ENABLED", False)
    for spec in sut.tools.active_specs(True):
        assert "batch_id" not in spec["input_schema"].get("properties", {}), spec["name"]


def test_withholding_it_does_not_mutate_shared_state(sut, monkeypatch):
    """TOOL_SPECS is module-level; a demo session must not narrow another's view."""
    monkeypatch.setattr(sut, "JOBS_SUBMIT_ENABLED", False)
    sut.tools.active_specs(False)
    shared = next(s for s in sut.TOOL_SPECS if s["name"] == "read_file")
    assert "batch_id" in shared["input_schema"]["properties"]


# --------------------------------------------------------------------------- #
# The copy-out line
# --------------------------------------------------------------------------- #
def test_the_copy_command_targets_the_readers_own_root(sut, batch):
    row = sut.jobs.batch_by_id(batch["id"])
    line = sut.results.copy_command(row, "U0SAM")
    assert line.startswith(f"cp -r {batch['dir']}")
    assert line.endswith(f"{batch['root'] / 'thermolysin'}/")


def test_someone_with_no_root_is_not_told_to_copy_into_a_stranger_s_tree(sut, batch):
    """Rootless means there is no default; guessing one would name someone else's
    files as though they were theirs."""
    batch["env"].register(
        {"slack_user_id": "U0SAM", "alias": "sam", "display_name": "Sam",
         "role": "admin", "calc_root": str(batch["root"])},
        {"slack_user_id": "U0READER", "alias": "reader", "display_name": "Reader",
         "declined": {"calc_root": True}},
    )
    row = sut.jobs.batch_by_id(batch["id"])
    assert "<somewhere you keep results>" in sut.results.copy_command(row, "U0READER")
