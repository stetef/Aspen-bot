"""
Tests for the per-user input template library (``aspen/templates.py``).

A template is the reusable half of "run the usual thing on this structure". It is
deliberately built like a workflow, so the properties under test are the ones that
already hold for workflows — asserted again here because this file's contents end
up on a compute node, which a workflow's never do:

* **Ownership comes from the Slack event.** No write path takes an owner.
* **Cross-readable, trust-tagged.** A colleague's template is `reference-only`.
* **Every overwrite is snapshotted.**
* **Validated in AND out.** A template that was acceptable when written must not
  stay acceptable forever just because it is already on disk.
"""

import pytest

GOOD = """\
!UKS B3LYP RIJCOSX Def2-TZVP tightscf
%pal nprocs 4 end
%maxcore 2000
* xyz 0 1
Fe 0.0 0.0 0.0
O  1.5 0.0 0.0
*
"""


@pytest.fixture
def two(sut, env):
    env.register(
        {"slack_user_id": "U0SAM", "alias": "sam", "display_name": "Sam", "role": "admin"},
        {"slack_user_id": "U01ARUN", "alias": "arun", "display_name": "Arun N."},
    )
    return env


# --------------------------------------------------------------------------- #
# Saving
# --------------------------------------------------------------------------- #
def test_saving_and_reading_back(sut, two):
    out = sut.templates.write("U01ARUN", "tddft-standard", GOOD,
                              description="TD-DFT for Fe complexes")
    assert "Saved" in out
    body = sut.templates.read("tddft-standard", "U01ARUN")
    assert "B3LYP" in body
    assert 'trust="your-own"' in body


def test_a_template_lands_in_its_owners_directory_found_by_id(sut, two):
    sut.templates.write("U01ARUN", "t1", GOOD)
    directory = sut.templates.dir_for("U01ARUN")
    assert directory.name == "arun__U01ARUN"
    assert (directory / "t1.inp").is_file()
    # A rename must not orphan it — lookups are by ID.
    two.register({"slack_user_id": "U01ARUN", "alias": "arun-a", "display_name": "Arun N."})
    assert sut.templates.dir_for("U01ARUN") == directory


def test_an_overwrite_is_snapshotted(sut, two):
    sut.templates.write("U01ARUN", "t1", GOOD)
    out = sut.templates.write("U01ARUN", "t1", GOOD.replace("B3LYP", "CAM-B3LYP"))
    assert "Updated" in out and "snapshot" in out
    history = list((sut.TEMPLATES_HISTORY_ROOT / "U01ARUN").glob("t1-*.inp"))
    assert len(history) == 1
    assert "B3LYP" in history[0].read_text() and "CAM-B3LYP" not in history[0].read_text()


@pytest.mark.parametrize("bad", [
    "", "  ", "../escape", "/abs", "Has Spaces", "dots.in.name",
    "trailing-", "-leading", "a" * 60, "semi;colon",
])
def test_unusable_names_are_refused(sut, two, bad):
    assert sut.templates.validate_name(bad) is not None
    assert "Error" in sut.templates.write("U01ARUN", bad, GOOD)


def test_names_are_normalised_to_lowercase(sut, two):
    """"TDDFT-Standard" and "tddft-standard" are one template, not two near-dupes."""
    sut.templates.write("U01ARUN", "TDDFT-Standard", GOOD)
    assert (sut.templates.dir_for("U01ARUN") / "tddft-standard.inp").is_file()
    assert 'trust="your-own"' in sut.templates.read("tddft-standard", "U01ARUN")
    # ...and saving under the other casing updates rather than duplicating.
    out = sut.templates.write("U01ARUN", "tddft-standard", GOOD.replace("B3LYP", "PBE0"))
    assert "Updated" in out
    assert len(list((sut.templates.dir_for("U01ARUN")).glob("*.inp"))) == 1


def test_a_traversing_name_cannot_escape_the_library(sut, two):
    """Belt and braces on top of the name regex."""
    for bad in ("../../etc/passwd", "..", "x/../../y"):
        assert "Error" in sut.templates.write("U01ARUN", bad, GOOD)
    root = sut.TEMPLATES_ROOT
    stray = [p for p in root.rglob("*") if p.is_file() and "arun__U01ARUN" not in str(p)]
    assert stray == [], f"files escaped the library: {stray}"


def test_an_empty_or_oversized_template_is_refused(sut, two, monkeypatch):
    assert "Error" in sut.templates.write("U01ARUN", "t1", "")
    monkeypatch.setattr(sut, "MAX_TEMPLATE_BYTES", 10)
    assert "Error" in sut.templates.write("U01ARUN", "t2", GOOD)


def test_an_unregistered_user_cannot_save(sut, two):
    assert "Error" in sut.templates.write("UVISITOR", "t1", GOOD)


def test_an_unsupported_code_is_refused(sut, two):
    """No validator means no template, so the guardrail is never silently absent."""
    out = sut.templates.write("U01ARUN", "t1", GOOD, code="gaussian")
    assert "Error" in out and "validator" in out


# --------------------------------------------------------------------------- #
# Validated in, and validated out
# --------------------------------------------------------------------------- #
def test_a_dangerous_template_is_refused_on_the_way_in(sut, two):
    out = sut.templates.write("U01ARUN", "evil", GOOD + '\n%compound\nend\n')
    assert "Error" in out
    assert not (sut.templates.dir_for("U01ARUN") / "evil.inp").exists()


def test_a_stored_template_is_revalidated_on_read_path(sut, two, monkeypatch):
    """The list of denied directives can grow after a template was saved.

    A file that was acceptable last month must not be trusted purely because it is
    already on disk — otherwise the validator only ever protects new writes.
    """
    sut.templates.write("U01ARUN", "t1", GOOD)
    content, meta = sut.templates.resolve("t1", "U01ARUN")
    # Simulate the denied list growing to cover something this template uses.
    monkeypatch.setattr(sut.inputs, "_ORCA_BLOCKS_DENIED",
                        {"pal": "newly considered dangerous"}, raising=False)
    assert sut.inputs.validate(content), (
        "a stored template must be re-checked against the CURRENT rules"
    )


# --------------------------------------------------------------------------- #
# Ownership and trust
# --------------------------------------------------------------------------- #
def test_no_write_path_takes_an_owner(sut):
    """C9 for templates: the destination is the Slack event's user, always."""
    import inspect
    sig = inspect.signature(sut.templates.write)
    assert "owner" not in sig.parameters
    assert list(sig.parameters)[0] == "uid"


def test_a_colleagues_template_is_reference_only(sut, two):
    sut.templates.write("U0SAM", "house-style", GOOD, description="how Sam runs these")
    body = sut.templates.read("house-style", "U01ARUN")
    assert 'trust="reference-only"' in body
    assert "save any result to THEIR OWN template" in body


def test_saving_cannot_overwrite_a_colleagues_template(sut, two):
    sut.templates.write("U0SAM", "shared-name", GOOD, description="sam's")
    sut.templates.write("U01ARUN", "shared-name", GOOD.replace("B3LYP", "PBE0"),
                        description="arun's")
    sam_body, _ = sut.templates.resolve("shared-name", "U0SAM", owner="sam")
    arun_body, _ = sut.templates.resolve("shared-name", "U01ARUN", owner="arun")
    assert "B3LYP" in sam_body and "PBE0" not in sam_body
    assert "PBE0" in arun_body


def test_your_own_template_wins_a_name_collision(sut, two):
    sut.templates.write("U0SAM", "dupe", GOOD, description="sam's")
    sut.templates.write("U01ARUN", "dupe", GOOD.replace("B3LYP", "PBE0"))
    content, meta = sut.templates.resolve("dupe", "U01ARUN")
    assert "PBE0" in content and meta["owner_id"] == "U01ARUN"


def test_identity_fields_are_stamped_not_taken_from_input(sut, two):
    sut.templates.write("U01ARUN", "t1", GOOD, description="mine",
                        derived_from="nobody-real")
    _, meta = sut.templates.resolve("t1", "U01ARUN")
    assert meta["owner_id"] == "U01ARUN"
    assert meta["owner_alias"] == "arun"
    # An unresolvable derived_from is dropped rather than recorded as a claim.
    assert meta["derived_from"] == ""

    sut.templates.write("U01ARUN", "t2", GOOD, derived_from="sam")
    _, meta2 = sut.templates.resolve("t2", "U01ARUN")
    assert meta2["derived_from"] == "sam"


# --------------------------------------------------------------------------- #
# Discovery
# --------------------------------------------------------------------------- #
def test_the_index_is_metadata_only_and_marks_your_own(sut, two):
    sut.templates.write("U0SAM", "sam-one", GOOD, description="sam's protocol")
    sut.templates.write("U01ARUN", "arun-one", GOOD, description="arun's protocol")
    entries = sut.templates.index("U01ARUN")
    by_name = {e["name"]: e for e in entries}
    assert by_name["arun-one"]["mine"] is True
    assert by_name["sam-one"]["mine"] is False
    # Bodies are fetched on demand, not carried in the index.
    assert all("content" not in e for e in entries)


def test_the_preamble_lists_templates_compactly(sut, two):
    sut.templates.write("U01ARUN", "tddft-standard", GOOD, description="TD-DFT for Fe")
    lines = sut.templates.preamble_lines("U01ARUN")
    assert any("tddft-standard" in ln and "yours" in ln for ln in lines)


def test_an_unknown_template_says_how_to_look(sut, two):
    out = sut.templates.read("nope", "U01ARUN")
    assert "Error" in out and "list_input_templates" in out


# --------------------------------------------------------------------------- #
# Deleting
# --------------------------------------------------------------------------- #
def test_deleting_snapshots_first(sut, two):
    sut.templates.write("U01ARUN", "t1", GOOD)
    out = sut.templates.delete("U01ARUN", "t1")
    assert "Deleted" in out
    assert not (sut.templates.dir_for("U01ARUN") / "t1.inp").exists()
    assert list((sut.TEMPLATES_HISTORY_ROOT / "U01ARUN").glob("t1-*.inp"))


def test_you_cannot_delete_what_you_do_not_have(sut, two):
    sut.templates.write("U0SAM", "sams", GOOD)
    out = sut.templates.delete("U01ARUN", "sams")
    assert "Error" in out
    # Sam's is untouched.
    assert (sut.templates.dir_for("U0SAM") / "sams.inp").is_file()


# --------------------------------------------------------------------------- #
# Placement
# --------------------------------------------------------------------------- #
def test_templates_live_outside_the_workspace(sut, env):
    """They are read back to build a job, so a sandbox-writable library would be an
    injection path with a compute node on the end of it."""
    from aspen import main
    assert not main._under(sut.TEMPLATES_ROOT, sut.WORKSPACE_ROOT)
    assert not main._under(sut.TEMPLATES_HISTORY_ROOT, sut.WORKSPACE_ROOT)


@pytest.mark.parametrize("name", ["TEMPLATES_ROOT", "TEMPLATES_HISTORY_ROOT"])
def test_a_template_path_in_the_workspace_is_fatal(sut, env, monkeypatch, name):
    monkeypatch.setattr(sut, "SANDBOX_WRITE_PATHS", [], raising=False)
    env.register({"slack_user_id": "U0SAM", "alias": "sam", "display_name": "Sam",
                  "calc_root": str(env.root("sam-calcs"))})
    monkeypatch.setattr(sut, name, sut.WORKSPACE_ROOT / "leaked")
    with pytest.raises(SystemExit):
        sut._check_state_locations()
