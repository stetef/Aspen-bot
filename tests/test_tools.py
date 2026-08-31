"""Characterization tests for the read-only file tools and the tool-server bridge."""

import httpx
import pytest


# --------------------------------------------------------------------------- #
# _safe_path
# --------------------------------------------------------------------------- #
def test_safe_path_allows_paths_inside_root(sut):
    p = sut._safe_path("sub/dir")
    assert p is not None
    assert str(p).startswith(str(sut.CALCULATIONS_ROOT))


def test_safe_path_rejects_parent_traversal(sut):
    assert sut._safe_path("../../etc/passwd") is None


def test_safe_path_rejects_absolute_escape(sut):
    assert sut._safe_path("/etc/passwd") is None


# --------------------------------------------------------------------------- #
# _list_directory
# --------------------------------------------------------------------------- #
def test_list_directory_sorts_dirs_before_files(sut):
    base = sut.CALCULATIONS_ROOT / "listtest"
    base.mkdir(parents=True)
    (base / "subdir").mkdir()
    (base / "data.txt").write_text("x")

    out = sut._list_directory("listtest")

    assert "Contents of 'listtest' (2 entries):" in out
    assert "[dir] subdir" in out
    assert "[file] data.txt" in out
    # directories sort ahead of files
    assert out.index("[dir] subdir") < out.index("[file] data.txt")


def test_list_directory_empty(sut):
    (sut.CALCULATIONS_ROOT / "emptydir").mkdir()
    assert sut._list_directory("emptydir") == "'emptydir' is empty."


def test_list_directory_missing(sut):
    assert sut._list_directory("nope") == "Error: 'nope' does not exist."


def test_list_directory_not_a_directory(sut):
    (sut.CALCULATIONS_ROOT / "afile").write_text("x")
    assert sut._list_directory("afile") == "Error: 'afile' is not a directory."


def test_list_directory_outside_root(sut):
    assert sut._list_directory("../escape") == "Error: '../escape' is outside the allowed directory."


# --------------------------------------------------------------------------- #
# _read_file
# --------------------------------------------------------------------------- #
def test_read_file_returns_contents(sut):
    (sut.CALCULATIONS_ROOT / "hello.txt").write_text("hello world")
    out = sut._read_file("hello.txt")
    # The size is stated on every read, so a capped one can never pass for whole.
    assert out == "--- hello.txt (11 bytes) ---\nhello world"


def test_read_file_truncates_at_limit(sut, monkeypatch):
    monkeypatch.setattr(sut, "MAX_FILE_BYTES", 5)
    (sut.CALCULATIONS_ROOT / "big.txt").write_text("0123456789")  # 10 bytes
    out = sut._read_file("big.txt")
    assert "10 bytes" in out                       # the true size, not what we got
    assert "NOT read" in out


def test_read_file_oversized_returns_both_ends(sut, monkeypatch):
    """The ORCA shape: settings at the head, the verdict at the tail.

    A head-only read of a long log returns everything except the answer while
    looking complete, which is how a converged run got reported as unconverged.
    """
    monkeypatch.setattr(sut, "MAX_FILE_BYTES", 200)
    body = ("CHARGE=0 MULTIPLICITY=1\n" + "filler line\n" * 400
            + "THE OPTIMIZATION HAS CONVERGED\n****ORCA TERMINATED NORMALLY****\n")
    (sut.CALCULATIONS_ROOT / "orca.log").write_text(body)
    out = sut._read_file("orca.log")
    assert "CHARGE=0 MULTIPLICITY=1" in out        # head: how the run was set up
    assert "ORCA TERMINATED NORMALLY" in out       # tail: how it ended
    assert "bytes skipped" in out                  # and the gap is admitted


def test_read_file_section_tail_spends_budget_at_the_end(sut, monkeypatch):
    monkeypatch.setattr(sut, "MAX_FILE_BYTES", 120)
    (sut.CALCULATIONS_ROOT / "t.log").write_text(
        "HEAD MARKER\n" + "x" * 4000 + "\nFinal Gibbs free energy  -1.5 Eh\n")
    out = sut._read_file("t.log", section="tail")
    assert "Final Gibbs free energy" in out
    assert "HEAD MARKER" not in out
    assert "NOT read" in out


def test_read_file_rejects_unknown_section(sut):
    (sut.CALCULATIONS_ROOT / "s.txt").write_text("x")
    assert "section must be" in sut._read_file("s.txt", section="middle")


def test_read_file_says_when_a_file_is_empty(sut):
    """A 0-byte file must say so; a bare header reads as 'nothing to report'."""
    (sut.CALCULATIONS_ROOT / "empty.sout").write_text("")
    out = sut._read_file("empty.sout")
    assert "0 bytes" in out and "empty" in out


def test_read_file_missing(sut):
    assert sut._read_file("ghost.txt") == "Error: 'ghost.txt' does not exist."


def test_read_file_on_directory(sut):
    (sut.CALCULATIONS_ROOT / "adir").mkdir()
    assert sut._read_file("adir") == "Error: 'adir' is not a regular file."


def test_read_file_outside_root(sut):
    assert sut._read_file("../secret") == "Error: '../secret' is outside the allowed directory."


# --------------------------------------------------------------------------- #
# _search_files
# --------------------------------------------------------------------------- #
def test_search_files_finds_content_match(sut):
    proj = sut.CALCULATIONS_ROOT / "srch"
    proj.mkdir()
    (proj / "a.log").write_text("line one\nSCF not converged\nline three\n")
    (proj / "b.log").write_text("all good here\n")
    out = sut._search_files("not converged", "srch")
    assert "srch/a.log:2:" in out
    assert "SCF not converged" in out
    assert "b.log" not in out


def test_search_files_no_match_reports_count(sut):
    proj = sut.CALCULATIONS_ROOT / "srch_none"
    proj.mkdir()
    (proj / "x.txt").write_text("nothing interesting\n")
    out = sut._search_files("zzz-absent", "srch_none")
    assert out.startswith("No matches for 'zzz-absent'")


def test_search_files_regex_mode(sut):
    proj = sut.CALCULATIONS_ROOT / "srch_re"
    proj.mkdir()
    (proj / "e.txt").write_text("energy = -1234.56 eV\n")
    out = sut._search_files(r"-\d+\.\d+", "srch_re", regex=True)
    assert "e.txt:1:" in out


def test_search_files_rejects_traversal(sut):
    out = sut._search_files("anything", "../..")
    assert "outside the allowed directory" in out


def test_search_files_cannot_read_outside_root(sut, tmp_path):
    # A secret outside the calculations root must never surface in results, even
    # though the bot's user can read it — the tool is fenced to the root.
    secret = tmp_path / "outside_secret.txt"
    secret.write_text("TOPSECRET_TOKEN_42\n")
    (sut.CALCULATIONS_ROOT / "inside.txt").write_text("ordinary data\n")
    out = sut._search_files("TOPSECRET_TOKEN_42", ".")
    # A no-match reply echoes the query, so check the secret FILE wasn't reached:
    assert out.startswith("No matches")
    assert "outside_secret" not in out


def test_search_files_reads_a_named_file_whole(sut, monkeypatch):
    """The regression: a match past the sweep's byte cap must still be found.

    A real ORCA log put 'THE OPTIMIZATION HAS CONVERGED' at line 94356 of a
    7.4 MB file. The cap read the first 30%, the search reported "No matches",
    and the agent told a user their calculations had not converged. Naming one
    file bounds the work already, so it is scanned in full.
    """
    monkeypatch.setattr(sut, "SEARCH_MAX_FILE_BYTES", 500)
    proj = sut.CALCULATIONS_ROOT / "big_log"
    proj.mkdir()
    log = proj / "run.log"
    log.write_text("filler\n" * 500 + "THE OPTIMIZATION HAS CONVERGED\n")
    out = sut._search_files("THE OPTIMIZATION HAS CONVERGED", "big_log/run.log")
    assert "run.log:501:" in out
    assert "INCOMPLETE" not in out              # nothing was hidden, so say nothing


def test_search_files_discloses_the_byte_cap_on_a_sweep(sut, monkeypatch):
    """A capped sweep may not report a false negative as a fact."""
    monkeypatch.setattr(sut, "SEARCH_MAX_FILE_BYTES", 500)
    proj = sut.CALCULATIONS_ROOT / "swept"
    proj.mkdir()
    (proj / "run.log").write_text("filler\n" * 500 + "TERMINATED NORMALLY\n")
    out = sut._search_files("TERMINATED NORMALLY", "swept")
    assert out.startswith("No matches")          # still capped: that is the budget
    assert "INCOMPLETE" in out                   # but it no longer reads as absence
    assert "run.log" in out                      # and it names what it could not finish


def test_search_files_partial_hits_are_flagged_too(sut, monkeypatch):
    """Truncated results are as misleading as empty ones — 17 hits out of 51."""
    monkeypatch.setattr(sut, "SEARCH_MAX_FILE_BYTES", 200)
    proj = sut.CALCULATIONS_ROOT / "partial"
    proj.mkdir()
    (proj / "e.log").write_text("ENERGY here\n" * 200)
    out = sut._search_files("ENERGY", "partial")
    assert "match(es)" in out
    assert "INCOMPLETE" in out


def test_search_files_skips_binary_when_named_directly(sut):
    """The single-file path detects binary on a probe, not the whole read."""
    proj = sut.CALCULATIONS_ROOT / "named_bin"
    proj.mkdir()
    (proj / "data.gbw").write_bytes(b"\x00\x01PATTERN" + b"\x00" * 100)
    out = sut._search_files("PATTERN", "named_bin/data.gbw")
    assert out.startswith("No matches")


def test_search_files_skips_binary(sut):
    proj = sut.CALCULATIONS_ROOT / "srch_bin"
    proj.mkdir()
    (proj / "data.bin").write_bytes(b"\x00\x01PATTERN\x00")
    (proj / "notes.txt").write_text("PATTERN here\n")
    out = sut._search_files("PATTERN", "srch_bin")
    assert "notes.txt:1:" in out
    assert "data.bin" not in out


# --------------------------------------------------------------------------- #
# _attach_file
# --------------------------------------------------------------------------- #
def test_attach_file_returns_resolved_path(sut):
    (sut.CALCULATIONS_ROOT / "report.csv").write_text("a,b\n1,2\n")
    text, atts = sut._attach_file("report.csv")
    assert atts == [str(sut.CALCULATIONS_ROOT / "report.csv")]
    assert "Attached 'report.csv'" in text


def test_attach_file_outside_root(sut):
    text, atts = sut._attach_file("../../etc/passwd")
    assert atts == []
    assert text == "Error: '../../etc/passwd' is outside the allowed directory."


def test_attach_file_missing(sut):
    text, atts = sut._attach_file("nope.dat")
    assert atts == []
    assert text == "Error: 'nope.dat' does not exist."


def test_attach_file_on_directory(sut):
    (sut.CALCULATIONS_ROOT / "adir2").mkdir()
    text, atts = sut._attach_file("adir2")
    assert atts == []
    assert text == "Error: 'adir2' is not a regular file."


def test_attach_file_too_large(sut, monkeypatch):
    monkeypatch.setattr(sut, "MAX_ATTACHMENT_BYTES", 4)
    (sut.CALCULATIONS_ROOT / "big.bin").write_bytes(b"0123456789")  # 10 bytes
    text, atts = sut._attach_file("big.bin")
    assert atts == []
    assert "attachment limit" in text


def test_attach_file_drains_into_attachment_sink(sut):
    (sut.CALCULATIONS_ROOT / "out.json").write_text("{}")
    ctx = {"attachments": []}
    text = sut.dispatch("attach_file", {"path": "out.json"}, ctx)
    assert "Attached 'out.json'" in text
    assert ctx["attachments"] == [str(sut.CALCULATIONS_ROOT / "out.json")]


# --------------------------------------------------------------------------- #
# _write_metadata
#
# Metadata is a SIDECAR now (aspen/metadata.py): the agent writes nothing inside
# any calculations root, so these assert the note landed in Aspen's own area and
# the project directory was left alone. The old invariants — project must exist,
# no traversal, size-capped, backed up on overwrite — are unchanged.
# --------------------------------------------------------------------------- #
@pytest.fixture
def meta(sut, env):
    """Per-test metadata sidecar, so history counts don't leak between tests."""
    return env


def _sidecar(sut, project, uid="U1"):
    alias = sut.registry.by_id(uid)["alias"]
    return sut.METADATA_ROOT / f"{alias}__{uid}" / project / "metadata.md"


def test_write_metadata_creates_the_sidecar(sut, meta):
    (sut.CALCULATIONS_ROOT / "proj_a").mkdir()
    out = sut._write_metadata("proj_a", "# notes\nhello\n", "", "U1")
    assert out.startswith("Created metadata for")
    assert _sidecar(sut, "proj_a").read_text() == "# notes\nhello\n"


def test_write_metadata_never_touches_the_calculations_tree(sut, meta):
    """The whole point of the move: no write lands inside anyone's root."""
    proj = sut.CALCULATIONS_ROOT / "proj_d"
    proj.mkdir()
    (proj / "results.dat").write_text("precious")
    sut._write_metadata("proj_d", "meta", "", "U1")
    assert (proj / "results.dat").read_text() == "precious"
    assert list(proj.iterdir()) == [proj / "results.dat"]   # nothing was added


def test_write_metadata_overwrites_existing(sut, meta):
    (sut.CALCULATIONS_ROOT / "proj_b").mkdir()
    sut._write_metadata("proj_b", "old", "", "U1")
    out = sut._write_metadata("proj_b", "new contents", "", "U1")
    assert out.startswith("Updated metadata for")
    assert _sidecar(sut, "proj_b").read_text() == "new contents"


def test_write_metadata_backs_up_clobbered_version(sut, meta):
    """An overwrite snapshots the PRIOR content, so a careless replace is recoverable."""
    (sut.CALCULATIONS_ROOT / "proj_hist").mkdir()
    sut._write_metadata("proj_hist", "important notes", "", "U1")
    sut._write_metadata("proj_hist", "oops, replaced everything", "", "U1")

    alias = sut.registry.by_id("U1")["alias"]
    hist_dir = sut.METADATA_HISTORY_ROOT / f"{alias}__U1" / "proj_hist"
    backups = list(hist_dir.glob("*.md"))
    assert len(backups) == 1
    assert backups[0].read_text() == "important notes"


def test_metadata_history_is_keyed_by_owner_as_well_as_project(sut, meta):
    """Two people with the same project name must not share a history directory."""
    (sut.CALCULATIONS_ROOT / "shared_name").mkdir()
    for uid in ("U1", "U2"):
        sut._write_metadata("shared_name", f"first from {uid}", "", uid)
        sut._write_metadata("shared_name", f"second from {uid}", "", uid)
    for uid in ("U1", "U2"):
        alias = sut.registry.by_id(uid)["alias"]
        hist = sut.METADATA_HISTORY_ROOT / f"{alias}__{uid}" / "shared_name"
        backups = list(hist.glob("*.md"))
        assert len(backups) == 1
        assert backups[0].read_text() == f"first from {uid}"


def test_write_metadata_create_does_not_back_up(sut, meta):
    (sut.CALCULATIONS_ROOT / "proj_new").mkdir()
    sut._write_metadata("proj_new", "# fresh\n", "", "U1")
    assert not sut.METADATA_HISTORY_ROOT.exists()


def test_write_metadata_rejects_missing_project(sut, meta):
    out = sut._write_metadata("ghost_proj", "x", "", "U1")
    assert "does not exist" in out
    assert not (sut.CALCULATIONS_ROOT / "ghost_proj").exists()


def test_write_metadata_rejects_nested_project_path(sut, meta):
    (sut.CALCULATIONS_ROOT / "proj_c").mkdir()
    out = sut._write_metadata("proj_c/sub", "x", "", "U1")
    assert "not a valid project name" in out


def test_write_metadata_rejects_parent_traversal(sut, meta):
    out = sut._write_metadata("..", "x", "", "U1")
    assert "not a valid project name" in out


def test_write_metadata_rejects_absolute_project(sut, meta):
    out = sut._write_metadata("/etc", "x", "", "U1")
    assert "not a valid project name" in out


def test_write_metadata_rejects_oversized_content(sut, meta, monkeypatch):
    monkeypatch.setattr(sut, "MAX_FILE_BYTES", 5)
    (sut.CALCULATIONS_ROOT / "proj_e").mkdir()
    out = sut._write_metadata("proj_e", "way too long", "", "U1")
    assert "over the" in out and "metadata limit" in out
    assert not _sidecar(sut, "proj_e").exists()


def test_write_metadata_dispatch_returns_text_no_attachments(sut, meta):
    (sut.CALCULATIONS_ROOT / "proj_f").mkdir()
    ctx = {"attachments": [], "user_id": "U1"}
    text = sut.dispatch("write_metadata", {"project": "proj_f", "content": "hi"}, ctx)
    assert text.startswith("Created metadata for")
    assert ctx["attachments"] == []


def test_read_metadata_round_trips_and_says_who_wrote_it(sut, meta):
    (sut.CALCULATIONS_ROOT / "proj_r").mkdir()
    sut._write_metadata("proj_r", "libraries: numpy", "", "U1")
    out = sut._read_metadata("proj_r", "", "U1")
    assert "libraries: numpy" in out
    assert 'written_by="aspen"' in out          # never mistakable for project data


def test_read_metadata_when_there_is_none(sut, meta):
    (sut.CALCULATIONS_ROOT / "proj_none").mkdir()
    assert "No metadata recorded" in sut._read_metadata("proj_none", "", "U1")


# --------------------------------------------------------------------------- #
# _call_tool_server
# --------------------------------------------------------------------------- #
def _ctx():
    return {"user_id": "U1", "username": "", "thread_ts": "1.0"}


def test_call_tool_server_unconfigured_secret(sut, monkeypatch):
    monkeypatch.setattr(sut, "AGENT_INTERNAL_SECRET", "")
    text, figs = sut._call_tool_server({"project_name": "p"}, _ctx())
    assert text == "Error: AGENT_INTERNAL_SECRET not configured — tool server unavailable."
    assert figs == []


def test_call_tool_server_success(sut, monkeypatch):
    class FakeResp:
        status_code = 200
        is_success = True

        def json(self):
            return {
                "status": "success",
                "duration_seconds": 1.5,
                "stdout": "hello output",
                "figures": ["/workspace/figures/a.png"],
            }

    monkeypatch.setattr(sut, "_tool_server_post", lambda *a, **k: FakeResp())
    text, figs = sut._call_tool_server(
        {"project_name": "proj", "code": "x", "dataset": [], "question": "q"}, _ctx()
    )
    assert figs == ["/workspace/figures/a.png"]
    assert "Status: success" in text
    assert "hello output" in text


def test_call_tool_server_bad_request(sut, monkeypatch):
    class FakeResp:
        status_code = 400
        is_success = False
        text = "bad request"

        def json(self):
            return {"detail": "your code is unsafe"}

    monkeypatch.setattr(sut, "_tool_server_post", lambda *a, **k: FakeResp())
    text, figs = sut._call_tool_server({"project_name": "proj"}, _ctx())
    assert text == "Error: your code is unsafe"
    assert figs == []


def test_call_tool_server_connection_error(sut, monkeypatch):
    def _boom(*a, **k):
        raise httpx.ConnectError("socket not there")

    monkeypatch.setattr(sut, "_tool_server_post", _boom)
    text, figs = sut._call_tool_server({"project_name": "proj"}, _ctx())
    assert text.startswith("Error: tool server is not running")
    assert figs == []
