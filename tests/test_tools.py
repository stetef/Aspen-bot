"""Characterization tests for the read-only file tools and the tool-server bridge."""

import requests


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
        ok = True

        def json(self):
            return {
                "status": "success",
                "duration_seconds": 1.5,
                "stdout": "hello output",
                "figures": ["/workspace/figures/a.png"],
            }

    monkeypatch.setattr(sut.requests, "post", lambda *a, **k: FakeResp())
    text, figs = sut._call_tool_server(
        {"project_name": "proj", "code": "x", "dataset": [], "question": "q"}, _ctx()
    )
    assert figs == ["/workspace/figures/a.png"]
    assert "Status: success" in text
    assert "hello output" in text


def test_call_tool_server_bad_request(sut, monkeypatch):
    class FakeResp:
        status_code = 400
        ok = False
        text = "bad request"

        def json(self):
            return {"detail": "your code is unsafe"}

    monkeypatch.setattr(sut.requests, "post", lambda *a, **k: FakeResp())
    text, figs = sut._call_tool_server({"project_name": "proj"}, _ctx())
    assert text == "Error: your code is unsafe"
    assert figs == []


def test_call_tool_server_connection_error(sut, monkeypatch):
    def _boom(*a, **k):
        raise requests.exceptions.ConnectionError()

    monkeypatch.setattr(sut.requests, "post", _boom)
    text, figs = sut._call_tool_server({"project_name": "proj"}, _ctx())
    assert text.startswith("Error: tool server is not running")
    assert figs == []
