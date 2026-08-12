"""
The cancellation boundary (spec §19.5) — who may cancel what, and how it fails.

This is its own file because under the beta account model these checks are not
defense in depth. The bot runs as the developer, so Unix ownership protects
nothing: every job the developer ever submitted by hand is inside ``scancel``
range of the credentials the bot holds. ``resolve_cancellable`` is the only thing
between a confused or injected model and somebody's queue.

Three gates, each of which must fail closed:

1. the ID must be a ledger row belonging to the **requesting Slack ID**;
2. ``scontrol`` must report the job as owned by the Unix account Aspen runs as;
3. its ``WorkDir`` must resolve inside **that requester's own** staging tree.

Gate 3 is the one a hand-submitted job cannot satisfy, and the one that closes the
recycled-job-ID window — s3df's ``MaxJobId`` is 67 M and the counter resets on a
controller rebuild, so a stale ledger row really can point at a live unrelated job.
"""

import pytest


@pytest.fixture
def two_users(sut, env, monkeypatch):
    """Sam and Arun, each with one recorded job in their own staging tree."""
    env.register(
        {"slack_user_id": "U0SAM", "alias": "sam", "display_name": "Sam", "role": "admin"},
        {"slack_user_id": "U01ARUN", "alias": "arun", "display_name": "Arun N."},
    )
    monkeypatch.setattr(sut.jobs, "whoami", lambda: "botuser")

    made = {}
    for uid, alias, jid, project in (("U0SAM", "sam", "1001", "thermolysin"),
                                     ("U01ARUN", "arun", "2002", "azurin")):
        staging = sut.jobs.staging_dir_for(uid, "1723480000.1")
        staging.mkdir(parents=True, exist_ok=True)
        batch = sut.jobs.record_batch(
            slack_user_id=uid, alias=alias, thread_ts="1723480000.1", project=project,
            owner_scope=alias, template_mode="ca-fixed", staging_dir=staging,
            structures=1, argv=["xas-run-batch", str(staging)],
        )
        sut.jobs.record_jobs(batch, [{"job_id": jid, "kind": "orca",
                                      "job_name": f"orca-{project}",
                                      "work_dir": str(staging)}])
        made[uid] = {"batch": batch, "job_id": jid, "staging": staging}
    return made


def fake_scontrol(mapping):
    """Build a ``scontrol_job`` stub from ``{job_id: {...fields}}``."""
    def _stub(job_id):
        return mapping.get(str(job_id))
    return _stub


def live(work_dir, user="botuser", state="RUNNING", **extra):
    return {"UserId": f"{user}(1001)", "WorkDir": str(work_dir),
            "JobState": state, **extra}


# --------------------------------------------------------------------------- #
# Gate 1 — the ledger, filtered to the requester
# --------------------------------------------------------------------------- #
def test_a_user_cannot_cancel_another_users_job_by_id(sut, two_users, monkeypatch):
    """The headline property. Slurm cannot tell these apart — they are one Unix user."""
    arun_job = two_users["U01ARUN"]
    monkeypatch.setattr(sut.jobs, "scontrol_job", fake_scontrol({
        arun_job["job_id"]: live(arun_job["staging"]),
    }))

    approved, refused = sut.jobs.resolve_cancellable("U0SAM", arun_job["job_id"])
    assert approved == [], "Sam must not be able to cancel Arun's job by naming its ID"
    assert refused == [], "and it should not even be a candidate to refuse"


def test_a_user_cannot_cancel_another_users_batch_or_project(sut, two_users, monkeypatch):
    arun = two_users["U01ARUN"]
    monkeypatch.setattr(sut.jobs, "scontrol_job", fake_scontrol({
        arun["job_id"]: live(arun["staging"]),
    }))
    for selector in (arun["batch"], "azurin"):
        approved, _ = sut.jobs.resolve_cancellable("U0SAM", selector)
        assert approved == [], f"selector {selector!r} must not reach another user"


def test_a_selector_can_only_narrow(sut, two_users, monkeypatch):
    sam = two_users["U0SAM"]
    monkeypatch.setattr(sut.jobs, "scontrol_job", fake_scontrol({
        sam["job_id"]: live(sam["staging"]),
    }))
    everything, _ = sut.jobs.resolve_cancellable("U0SAM", "all")
    narrowed, _ = sut.jobs.resolve_cancellable("U0SAM", sam["job_id"])
    assert len(everything) == 1 and len(narrowed) == 1
    # Nonsense selectors resolve to nothing rather than to everything.
    for selector in ("*", "", "%", "1 OR 1=1", "../../"):
        got, _ = sut.jobs.resolve_cancellable("U0SAM", selector)
        assert len(got) <= len(everything)


def test_an_unknown_job_id_is_never_cancellable(sut, two_users, monkeypatch):
    """A hallucinated ID, or one a user typed in chat, is not in the ledger."""
    monkeypatch.setattr(sut.jobs, "scontrol_job",
                        fake_scontrol({"999999": live("/anywhere")}))
    approved, refused = sut.jobs.resolve_cancellable("U0SAM", "999999")
    assert approved == [] and refused == []


# --------------------------------------------------------------------------- #
# Gate 2 — the job must be ours
# --------------------------------------------------------------------------- #
def test_a_job_owned_by_another_unix_user_is_refused(sut, two_users, monkeypatch):
    sam = two_users["U0SAM"]
    monkeypatch.setattr(sut.jobs, "scontrol_job", fake_scontrol({
        sam["job_id"]: live(sam["staging"], user="someone-else"),
    }))
    approved, refused = sut.jobs.resolve_cancellable("U0SAM", "all")
    assert approved == []
    assert "someone-else" in refused[0][1]


# --------------------------------------------------------------------------- #
# Gate 3 — WorkDir, the substitute for Unix ownership
# --------------------------------------------------------------------------- #
def test_a_recycled_job_id_is_refused_even_though_the_ledger_says_otherwise(
        sut, two_users, monkeypatch):
    """The reason verification runs against live Slurm rather than the ledger alone.

    Job IDs wrap. A months-old row can name an ID that now belongs to somebody's
    unrelated running job — it passes gate 1 by construction, and only the WorkDir
    check catches it.
    """
    sam = two_users["U0SAM"]
    monkeypatch.setattr(sut.jobs, "scontrol_job", fake_scontrol({
        sam["job_id"]: live("/home/someone/real-research"),
    }))
    approved, refused = sut.jobs.resolve_cancellable("U0SAM", "all")
    assert approved == []
    assert "outside your own staging area" in refused[0][1]


def test_a_hand_submitted_job_cannot_satisfy_the_fence(sut, two_users, monkeypatch):
    """The developer's own jobs are structurally uncancellable, not merely disallowed."""
    sam = two_users["U0SAM"]
    for work_dir in ("/sdf/home/t/tetef01/my-own-run", "/tmp", str(sam["staging"].parent.parent)):
        monkeypatch.setattr(sut.jobs, "scontrol_job",
                            fake_scontrol({sam["job_id"]: live(work_dir)}))
        approved, refused = sut.jobs.resolve_cancellable("U0SAM", "all")
        assert approved == [], f"WorkDir {work_dir} must not pass the fence"


def test_one_users_staging_is_not_inside_anothers(sut, two_users, monkeypatch):
    """Sibling directories, so Arun's WorkDir can never read as inside Sam's."""
    sam_fence = sut.jobs.user_staging_root("U0SAM")
    arun_staging = two_users["U01ARUN"]["staging"]
    assert not sut.jobs._within(arun_staging, sam_fence)

    # ...and asserted through the real seam, not just the helper.
    sam = two_users["U0SAM"]
    monkeypatch.setattr(sut.jobs, "scontrol_job", fake_scontrol({
        sam["job_id"]: live(arun_staging),
    }))
    approved, refused = sut.jobs.resolve_cancellable("U0SAM", "all")
    assert approved == [] and "outside your own staging area" in refused[0][1]


def test_a_symlink_out_of_staging_does_not_read_as_containment(sut, two_users, monkeypatch, tmp_path):
    """``_within`` resolves both sides, so a planted link cannot fake the fence."""
    sam = two_users["U0SAM"]
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    link = sam["staging"] / "escape"
    link.symlink_to(outside)

    monkeypatch.setattr(sut.jobs, "scontrol_job", fake_scontrol({
        sam["job_id"]: live(link),
    }))
    approved, _ = sut.jobs.resolve_cancellable("U0SAM", "all")
    assert approved == []


# --------------------------------------------------------------------------- #
# Failing closed
# --------------------------------------------------------------------------- #
def test_scontrol_failure_refuses_rather_than_falls_through(sut, two_users, monkeypatch):
    monkeypatch.setattr(sut.jobs, "scontrol_job", lambda job_id: None)
    approved, refused = sut.jobs.resolve_cancellable("U0SAM", "all")
    assert approved == []
    assert refused and "no current record" in refused[0][1]


def test_a_missing_workdir_refuses(sut, two_users, monkeypatch):
    sam = two_users["U0SAM"]
    monkeypatch.setattr(sut.jobs, "scontrol_job", fake_scontrol({
        sam["job_id"]: {"UserId": "botuser(1)", "JobState": "RUNNING"},
    }))
    approved, refused = sut.jobs.resolve_cancellable("U0SAM", "all")
    assert approved == []
    assert "no WorkDir" in refused[0][1]


def test_a_comment_naming_another_user_refuses(sut, two_users, monkeypatch):
    """Once the pipeline sets --comment it becomes a second check, not a replacement."""
    sam = two_users["U0SAM"]
    monkeypatch.setattr(sut.jobs, "scontrol_job", fake_scontrol({
        sam["job_id"]: live(sam["staging"], Comment="aspen/v1/U01ARUN/1723480000.1"),
    }))
    approved, refused = sut.jobs.resolve_cancellable("U0SAM", "all")
    assert approved == []
    assert "different user" in refused[0][1]


def test_a_matching_comment_passes(sut, two_users, monkeypatch):
    sam = two_users["U0SAM"]
    monkeypatch.setattr(sut.jobs, "scontrol_job", fake_scontrol({
        sam["job_id"]: live(sam["staging"], Comment="aspen/v1/U0SAM/1723480000.1"),
    }))
    approved, _ = sut.jobs.resolve_cancellable("U0SAM", "all")
    assert len(approved) == 1


def test_an_unregistered_user_cannot_cancel(sut, two_users):
    with pytest.raises(sut.jobs.JobsError):
        sut.jobs.resolve_cancellable("UVISITOR", "all")


# --------------------------------------------------------------------------- #
# The argv itself
# --------------------------------------------------------------------------- #
def test_scancel_argv_is_explicit_ids_only(sut):
    assert sut.jobs.build_scancel_argv(["123", "456_7"]) == ["scancel", "123", "456_7"]


def test_scancel_never_accepts_a_filter_flag(sut):
    """Every one of these delegates enumeration to Slurm, defeating verification.

    ``scancel -u tetef01`` is one word from the entire queue, so passing a filter
    is treated as a programming error worth raising on rather than something the
    builder merely happens not to emit.
    """
    for flag in sut.jobs.FORBIDDEN_SCANCEL_FLAGS:
        with pytest.raises(sut.jobs.JobsError):
            sut.jobs.build_scancel_argv([flag])
        with pytest.raises(sut.jobs.JobsError):
            sut.jobs.build_scancel_argv(["123", flag])


def test_scancel_rejects_anything_that_is_not_a_job_id(sut):
    for bad in ("tetef01", "--me", "; rm -rf /", "all", "*", "", "12;34", "-1"):
        with pytest.raises(sut.jobs.JobsError):
            sut.jobs.build_scancel_argv([bad])


def test_cancelling_nothing_is_an_error_not_an_empty_scancel(sut):
    """A bare ``scancel`` with no IDs would be a very bad thing to build."""
    with pytest.raises(sut.jobs.JobsError):
        sut.jobs.build_scancel_argv([])


# --------------------------------------------------------------------------- #
# End to end through the tool
# --------------------------------------------------------------------------- #
def test_cancel_runs_scancel_with_only_the_verified_ids(sut, two_users, monkeypatch):
    sam, arun = two_users["U0SAM"], two_users["U01ARUN"]
    monkeypatch.setattr(sut.jobs, "scontrol_job", fake_scontrol({
        sam["job_id"]: live(sam["staging"]),
        arun["job_id"]: live(arun["staging"]),
    }))
    ran = []

    class P:
        returncode, stdout, stderr = 0, "", ""

    monkeypatch.setattr(sut.jobs, "_run_slurm", lambda argv: (ran.append(argv), P())[1])

    result = sut.jobs.cancel("U0SAM", "all")
    assert result["ok"]
    assert ran == [["scancel", sam["job_id"]]], (
        "only Sam's verified job may appear in the argv"
    )
    assert arun["job_id"] not in ran[0]


def test_the_tool_requires_a_token_before_cancelling(sut, two_users, monkeypatch):
    """The preview call must not cancel. The confirmation is Python, not prompt."""
    sam = two_users["U0SAM"]
    monkeypatch.setattr(sut.jobs, "scontrol_job", fake_scontrol({
        sam["job_id"]: live(sam["staging"]),
    }))
    ran = []
    monkeypatch.setattr(sut.jobs, "_run_slurm", lambda argv: ran.append(argv))

    ctx = {"user_id": "U0SAM", "thread_ts": "1723480000.1", "attachments": []}
    text = sut.dispatch("cancel_orca_batch", {"selector": "all"}, ctx)

    assert ran == [], "the preview call must not cancel anything"
    assert "NOT cancelled yet" in text
    assert "confirm_token=" in text


def test_the_tool_reports_what_it_skipped(sut, two_users, monkeypatch):
    """A partial result must not read as a complete one."""
    sam = two_users["U0SAM"]
    monkeypatch.setattr(sut.jobs, "scontrol_job", fake_scontrol({
        sam["job_id"]: live("/somewhere/else"),
    }))
    ctx = {"user_id": "U0SAM", "thread_ts": "1723480000.1", "attachments": []}
    text = sut.dispatch("cancel_orca_batch", {"selector": "all"}, ctx)
    assert "Nothing was cancelled" in text
    assert "outside your own staging area" in text
