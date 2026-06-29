"""Characterization tests for the read-only file tools and the tool-server bridge."""

import httpx


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
    assert out == "--- hello.txt ---\nhello world"


def test_read_file_truncates_at_limit(sut, monkeypatch):
    monkeypatch.setattr(sut, "MAX_FILE_BYTES", 5)
    (sut.CALCULATIONS_ROOT / "big.txt").write_text("0123456789")  # 10 bytes
    out = sut._read_file("big.txt")
    assert out.startswith("--- big.txt ---\n01234")
    assert "[Truncated: showing first 5 of 10 bytes]" in out


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
# --------------------------------------------------------------------------- #
def test_write_metadata_creates_in_existing_project(sut):
    (sut.CALCULATIONS_ROOT / "proj_a").mkdir()
    out = sut._write_metadata("proj_a", "# notes\nhello\n")
    assert out.startswith("Created proj_a/metadata.md")
    assert (sut.CALCULATIONS_ROOT / "proj_a" / "metadata.md").read_text() == "# notes\nhello\n"


def test_write_metadata_overwrites_existing(sut):
    proj = sut.CALCULATIONS_ROOT / "proj_b"
    proj.mkdir()
    (proj / "metadata.md").write_text("old")
    out = sut._write_metadata("proj_b", "new contents")
    assert out.startswith("Updated proj_b/metadata.md")
    assert (proj / "metadata.md").read_text() == "new contents"


def test_write_metadata_backs_up_clobbered_version(sut):
    """An overwrite snapshots the PRIOR content to the workspace history dir, so a
    careless full-file replace is recoverable."""
    proj = sut.CALCULATIONS_ROOT / "proj_hist"
    proj.mkdir()
    (proj / "metadata.md").write_text("important notes")
    sut._write_metadata("proj_hist", "oops, replaced everything")

    hist_dir = sut.WORKSPACE_ROOT / "metadata_history" / "proj_hist"
    backups = list(hist_dir.glob("*.md"))
    assert len(backups) == 1
    assert backups[0].read_text() == "important notes"   # the clobbered version


def test_write_metadata_create_does_not_back_up(sut):
    """Creating a new metadata.md has nothing to clobber, so no backup is made."""
    proj = sut.CALCULATIONS_ROOT / "proj_new"
    proj.mkdir()
    sut._write_metadata("proj_new", "# fresh\n")
    assert not (sut.WORKSPACE_ROOT / "metadata_history" / "proj_new").exists()


def test_write_metadata_rejects_missing_project(sut):
    out = sut._write_metadata("ghost_proj", "x")
    assert "does not exist" in out
    assert not (sut.CALCULATIONS_ROOT / "ghost_proj").exists()


def test_write_metadata_rejects_nested_project_path(sut):
    (sut.CALCULATIONS_ROOT / "proj_c").mkdir()
    out = sut._write_metadata("proj_c/sub", "x")
    assert "not a valid project name" in out


def test_write_metadata_rejects_parent_traversal(sut):
    out = sut._write_metadata("..", "x")
    assert "not a valid project name" in out


def test_write_metadata_rejects_absolute_project(sut):
    out = sut._write_metadata("/etc", "x")
    assert "not a valid project name" in out


def test_write_metadata_does_not_write_other_files(sut):
    """A project dir becomes writable for metadata.md only — not its data files."""
    proj = sut.CALCULATIONS_ROOT / "proj_d"
    proj.mkdir()
    (proj / "results.dat").write_text("precious")
    sut._write_metadata("proj_d", "meta")
    # the only file the tool created is metadata.md; results.dat is untouched
    assert (proj / "results.dat").read_text() == "precious"
    assert (proj / "metadata.md").exists()


def test_write_metadata_rejects_oversized_content(sut, monkeypatch):
    monkeypatch.setattr(sut, "MAX_FILE_BYTES", 5)
    (sut.CALCULATIONS_ROOT / "proj_e").mkdir()
    out = sut._write_metadata("proj_e", "way too long")
    assert "over the" in out and "metadata limit" in out
    assert not (sut.CALCULATIONS_ROOT / "proj_e" / "metadata.md").exists()


def test_write_metadata_dispatch_returns_text_no_attachments(sut):
    (sut.CALCULATIONS_ROOT / "proj_f").mkdir()
    ctx = {"attachments": []}
    text = sut.dispatch("write_metadata", {"project": "proj_f", "content": "hi"}, ctx)
    assert text.startswith("Created proj_f/metadata.md")
    assert ctx["attachments"] == []


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
