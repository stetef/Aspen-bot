"""
Tests for ``aspen-users workflow`` — filing documents as workflows on someone's
behalf.

The invariants here differ from the Slack write path's:

* **The body is not edited.** People hand over prose that becomes their own
  standing guidance, so the import path stores it verbatim and only ever authors
  the one-line ``description``.
* **Filing for someone is not writing as them.** ``owner_id`` stays the user;
  ``updated_by`` records the admin who did it.
* **A description is never invented unattended.** ``--description`` wins over
  everything, ``--no-draft`` never calls out, and a draft is filed only on an
  explicit yes.
"""

import importlib

import pytest


@pytest.fixture
def wf(sut, tmp_path, monkeypatch):
    """Scratch registry + workflows tree with sam (admin), arun, and priya."""
    monkeypatch.setattr(sut, "USERS_FILE", tmp_path / "users.json")
    root = tmp_path / "workflows"
    root.mkdir()
    monkeypatch.setattr(sut, "WORKFLOWS_ROOT", root)
    # Per-test workspace: the session-scoped one would accumulate backup history
    # across tests and make the "how many backups" assertion lie.
    monkeypatch.setattr(sut, "WORKSPACE_ROOT", tmp_path / "workspace")
    sut.registry.invalidate()
    sut.registry.save([
        {"slack_user_id": "U0SAM", "alias": "sam", "display_name": "Sam",
         "role": "admin", "status": "active"},
        {"slack_user_id": "U01ARUN", "alias": "arun", "display_name": "Arun N.",
         "role": "member", "status": "active"},
        {"slack_user_id": "U0PRIYA", "alias": "priya", "display_name": "Priya P.",
         "role": "member", "status": "active"},
    ])
    yield sut.workflows
    sut.registry.invalidate()


@pytest.fixture
def cli(sut, monkeypatch):
    """The CLI module, with the description drafter wired to fail loudly.

    Imported through importlib rather than at module scope because importing
    ``aspen.*`` needs the environment the ``sut`` fixture sets up. The default
    stub means any test that reaches the drafter without saying so is a bug.
    """
    module = importlib.import_module("aspen.users_cli")

    def _no_subprocess(body):
        raise AssertionError("the drafter should not have been called")

    monkeypatch.setattr(module, "_draft_description", _no_subprocess)
    return module


@pytest.fixture
def unattended(monkeypatch):
    """No terminal: every prompt raises EOF, as a piped stdin does in production.

    pytest's captured stdin raises OSError rather than EOFError, so relying on it
    would test the harness instead of the CLI.
    """
    def _eof(*_args, **_kwargs):
        raise EOFError

    monkeypatch.setattr("builtins.input", _eof)


@pytest.fixture
def doc(tmp_path):
    """A handed-over document with no frontmatter — the common case."""
    path = tmp_path / "ArunDFTWorkflow.md"
    path.write_text(
        "**Arun's DFT Workflow**\n\n"
        "1. BP86/def2-TZVP for 3d transition metals.\n"
        "2. If the spin state is unknown, run each one separately.\n",
        encoding="utf-8",
    )
    return path


def _filed(sut, name="arun__U01ARUN"):
    return sut.WORKFLOWS_ROOT / name / "WORKFLOW.md"


# --------------------------------------------------------------------------- #
# import
# --------------------------------------------------------------------------- #
def test_import_files_the_document_under_the_user(sut, wf, cli, doc):
    code = cli.main(["workflow", "import", "arun", str(doc),
                     "--description", "ORCA DFT for 3d transition metals."])
    assert code == 0
    assert _filed(sut).is_file()


def test_import_stores_the_body_verbatim(sut, wf, cli, doc):
    """The whole point: someone's own words are filed, not rewritten."""
    cli.main(["workflow", "import", "arun", str(doc), "--description", "DFT."])
    _, body = wf.parse(_filed(sut).read_text())
    assert body.strip() == doc.read_text().strip()


def test_import_stamps_the_owner_but_credits_the_admin(sut, wf, cli, doc):
    cli.main(["--by", "sam@slac", "workflow", "import", "arun", str(doc),
              "--description", "DFT."])
    meta, _ = wf.parse(_filed(sut).read_text())
    assert meta["owner_id"] == "U01ARUN"
    assert meta["owner_name"] == "Arun N."
    assert meta["updated_by"] == "sam@slac"


def test_import_creates_the_directory_for_a_user_who_has_none(sut, wf, cli, doc):
    assert wf.dir_for("U0PRIYA") is None
    cli.main(["workflow", "import", "priya", str(doc), "--description", "DFT."])
    assert (sut.WORKFLOWS_ROOT / "priya__U0PRIYA" / "WORKFLOW.md").is_file()


def test_import_resolves_a_slack_id_too(sut, wf, cli, doc):
    assert cli.main(["workflow", "import", "U01ARUN", str(doc),
                     "--description", "DFT."]) == 0
    assert _filed(sut).is_file()


def test_the_description_becomes_the_routing_line(sut, wf, cli, doc):
    """What the admin types is what other turns see in the index."""
    cli.main(["workflow", "import", "arun", str(doc),
              "--description", "ORCA DFT: geometry optimization, spin states."])
    meta, _ = wf.parse(_filed(sut).read_text())
    assert meta["description"] == "ORCA DFT: geometry optimization, spin states."
    assert any(e["description"] == meta["description"] for e in wf.index())


def test_a_description_the_author_wrote_is_kept(sut, wf, cli, tmp_path):
    src = tmp_path / "with-frontmatter.md"
    src.write_text("---\ndescription: Their own words.\n---\n\nBody here.\n",
                   encoding="utf-8")
    cli.main(["workflow", "import", "arun", str(src)])   # drafter would assert
    meta, body = wf.parse(_filed(sut).read_text())
    assert meta["description"] == "Their own words."
    assert body.strip() == "Body here."


def test_no_draft_files_without_a_description_and_says_so(sut, wf, cli, doc, capsys):
    assert cli.main(["workflow", "import", "arun", str(doc), "--no-draft"]) == 0
    meta, _ = wf.parse(_filed(sut).read_text())
    assert not meta.get("description")
    assert "no `description:`" in capsys.readouterr().out


def test_a_drafted_description_is_offered_and_accepted_with_yes(
        sut, wf, cli, doc, monkeypatch, capsys):
    monkeypatch.setattr(cli, "_draft_description",
                        lambda body: ("Drafted from the body.", ""))
    assert cli.main(["workflow", "import", "arun", str(doc), "-y"]) == 0
    meta, _ = wf.parse(_filed(sut).read_text())
    assert meta["description"] == "Drafted from the body."
    assert "Drafted description:" in capsys.readouterr().out


def test_a_drafted_description_is_not_filed_unattended(
        sut, wf, cli, doc, unattended, monkeypatch):
    """Without -y and without a terminal, the draft is declined, not accepted."""
    monkeypatch.setattr(cli, "_draft_description", lambda body: ("Drafted.", ""))
    assert cli.main(["workflow", "import", "arun", str(doc)]) == 0
    meta, _ = wf.parse(_filed(sut).read_text())
    assert not meta.get("description")


def test_a_failed_draft_is_a_warning_not_a_failure(
        sut, wf, cli, doc, monkeypatch, capsys):
    monkeypatch.setattr(cli, "_draft_description", lambda body: ("", "claude not found"))
    assert cli.main(["workflow", "import", "arun", str(doc)]) == 0
    assert _filed(sut).is_file()
    assert "could not draft a description" in capsys.readouterr().out


def test_import_refuses_a_missing_file(sut, wf, cli, tmp_path):
    assert cli.main(["workflow", "import", "arun", str(tmp_path / "nope.md"),
                     "--description", "x"]) == 1


def test_import_refuses_an_empty_document(sut, wf, cli, tmp_path):
    src = tmp_path / "empty.md"
    src.write_text("---\ndescription: All header, no body.\n---\n\n   \n", encoding="utf-8")
    assert cli.main(["workflow", "import", "arun", str(src)]) == 1
    assert not _filed(sut).exists()


def test_import_refuses_an_unknown_user(sut, wf, cli, doc, capsys):
    assert cli.main(["workflow", "import", "nobody", str(doc), "--description", "x"]) == 1
    assert "Known aliases" in capsys.readouterr().err


def test_overwriting_needs_confirmation(sut, wf, cli, doc, tmp_path, unattended):
    cli.main(["workflow", "import", "arun", str(doc), "--description", "First."])
    other = tmp_path / "second.md"
    other.write_text("Replacement body.\n", encoding="utf-8")
    assert cli.main(["workflow", "import", "arun", str(other),
                     "--description", "Second."]) == 1
    assert "Replacement" not in _filed(sut).read_text()


def test_overwriting_with_yes_backs_up_the_previous_version(sut, wf, cli, doc, tmp_path):
    cli.main(["workflow", "import", "arun", str(doc), "--description", "First."])
    other = tmp_path / "second.md"
    other.write_text("Replacement body.\n", encoding="utf-8")
    assert cli.main(["workflow", "import", "arun", str(other),
                     "--description", "Second.", "-y"]) == 0
    assert "Replacement body." in _filed(sut).read_text()
    backups = list((sut.WORKSPACE_ROOT / "workflow_history" / "U01ARUN").glob("*.md"))
    assert len(backups) == 1


def test_archive_source_moves_the_original(sut, wf, cli, doc):
    cli.main(["workflow", "import", "arun", str(doc),
              "--description", "DFT.", "--archive-source"])
    assert not doc.exists()
    assert list(doc.parent.glob(f"{doc.name}.imported-*"))


def test_the_source_is_left_alone_by_default(sut, wf, cli, doc):
    cli.main(["workflow", "import", "arun", str(doc), "--description", "DFT."])
    assert doc.exists()


def test_import_can_file_the_group_workflow(sut, wf, cli, doc):
    assert cli.main(["workflow", "import", "_group", str(doc),
                     "--description", "House style."]) == 0
    assert (sut.WORKFLOWS_ROOT / "_group" / "WORKFLOW.md").is_file()
    assert 'trust="group-default"' in wf.read("_group", "U01ARUN")


def test_an_oversized_document_is_a_nonzero_exit(sut, wf, cli, tmp_path, monkeypatch):
    monkeypatch.setattr(sut, "MAX_WORKFLOW_BYTES", 100)
    src = tmp_path / "huge.md"
    src.write_text("x" * 500, encoding="utf-8")
    assert cli.main(["workflow", "import", "arun", str(src), "--description", "x"]) == 1
    assert not _filed(sut).exists()


# --------------------------------------------------------------------------- #
# describe
# --------------------------------------------------------------------------- #
def test_describe_swaps_the_line_and_leaves_the_body(sut, wf, cli, doc):
    cli.main(["workflow", "import", "arun", str(doc), "--description", "Old."])
    before = wf.parse(_filed(sut).read_text())[1]
    assert cli.main(["workflow", "describe", "arun", "New and sharper."]) == 0
    meta, after = wf.parse(_filed(sut).read_text())
    assert meta["description"] == "New and sharper."
    assert after.strip() == before.strip()


def test_describe_keeps_the_owner(sut, wf, cli, doc):
    cli.main(["workflow", "import", "arun", str(doc), "--description", "Old."])
    cli.main(["--by", "sam@slac", "workflow", "describe", "arun", "New."])
    meta, _ = wf.parse(_filed(sut).read_text())
    assert meta["owner_id"] == "U01ARUN"
    assert meta["updated_by"] == "sam@slac"


def test_describe_needs_a_workflow_to_describe(sut, wf, cli, capsys):
    assert cli.main(["workflow", "describe", "priya", "Anything."]) == 1
    assert "import one first" in capsys.readouterr().err


def test_describe_refuses_to_blank_the_line(sut, wf, cli, doc):
    cli.main(["workflow", "import", "arun", str(doc), "--description", "Keep me."])
    assert cli.main(["workflow", "describe", "arun", "   "]) == 1
    meta, _ = wf.parse(_filed(sut).read_text())
    assert meta["description"] == "Keep me."


# --------------------------------------------------------------------------- #
# list / show
# --------------------------------------------------------------------------- #
def test_list_shows_descriptions_and_who_is_missing(sut, wf, cli, doc, capsys):
    cli.main(["workflow", "import", "arun", str(doc), "--description", "ORCA DFT runs."])
    assert cli.main(["workflow", "list"]) == 0
    out = capsys.readouterr().out
    assert "ORCA DFT runs." in out
    assert "priya" in out.split("No workflow yet:")[1]


def test_list_is_empty_but_helpful(sut, wf, cli, capsys):
    assert cli.main(["workflow", "list"]) == 0
    assert "No workflows on file" in capsys.readouterr().out


def test_show_prints_the_body_and_the_path(sut, wf, cli, doc, capsys):
    cli.main(["workflow", "import", "arun", str(doc), "--description", "DFT."])
    assert cli.main(["workflow", "show", "arun"]) == 0
    out = capsys.readouterr().out
    assert "BP86/def2-TZVP" in out
    assert str(_filed(sut)) in out


def test_show_of_a_user_without_one(sut, wf, cli):
    assert cli.main(["workflow", "show", "priya"]) == 1
