"""
Tests for the conversation reader (``dashboard/transcripts.py``).

This module is a **third reader of on-disk state**, and the riskiest of them: it
parses a file format that belongs to the Claude Code CLI rather than to Aspen, so
nothing in this repo fails loudly when that format shifts. The tests here are
therefore shaped around the two things that would actually hurt.

The first is showing text that should not be shown. A transcript holds both sides
of every turn no matter what the telemetry content switch said, so it is a way to
walk straight around a redaction — and unlike a missing chart, nobody would
notice. Most of this file is that gate.

The second is picking up sessions that are not Aspen's. The bot's transcripts
land in the same tree as the operator's own Claude Code sessions, keyed only by
working directory, so "every jsonl under ~/.claude/projects" is not the same set
as "every Aspen conversation".
"""

import importlib.util
import json
from pathlib import Path

import pytest

pd = pytest.importorskip(
    "pandas", reason="dashboard tests need pandas — see requirements-dev.txt")


def _load(name: str):
    path = Path(__file__).resolve().parent.parent / "dashboard" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"aspen_dashboard_{name}", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


T = _load("transcripts")
dataio = _load("data")

CONTEXT = ("<aspen_context>\nThe person speaking to you right now is "
           "Chris Ohmer (alias `chris-ohmer`, Slack ID U08B).\n</aspen_context>\n\n")


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _line(kind, content, ts="2026-08-31T18:02:46.000Z"):
    return json.dumps({"type": kind, "timestamp": ts, "message": {"content": content}})


def _transcript(projects, session_id, *events, project="-home-bot"):
    directory = projects / project
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{session_id}.jsonl"
    path.write_text("\n".join(events) + "\n")
    return path


def _exchange(question, reply, ts="2026-08-31T18:02:46.000Z", alias_block=CONTEXT):
    return [_line("user", alias_block + question, ts),
            _line("assistant", [{"type": "text", "text": reply}], ts)]


# --------------------------------------------------------------------------- #
# Finding Aspen's sessions among everyone else's
# --------------------------------------------------------------------------- #
def test_reads_a_conversation_with_both_sides(tmp_path):
    _transcript(tmp_path, "s1", *_exchange("did it converge?", "Yes — all eight."))
    sessions = T.read_all(tmp_path)
    assert len(sessions) == 1
    turn = sessions[0]["turns"][0]
    assert turn["question"] == "did it converge?"
    assert turn["reply"] == ["Yes — all eight."]
    assert turn["who"] == "chris-ohmer"


def test_ignores_sessions_that_are_not_aspens(tmp_path):
    """The operator's own Claude Code sessions live in the same tree."""
    _transcript(tmp_path, "mine", _line("user", "refactor this module"),
                _line("assistant", [{"type": "text", "text": "sure"}]))
    assert T.read_all(tmp_path) == []


def test_finds_sessions_under_any_project_directory(tmp_path):
    """The slug follows the bot's cwd, which moves when the sandbox goes on."""
    _transcript(tmp_path, "a", *_exchange("q1", "a1"), project="-home-bot")
    _transcript(tmp_path, "b", *_exchange("q2", "a2"), project="-aspen-workspace")
    assert {s["session_id"] for s in T.read_all(tmp_path)} == {"a", "b"}


def test_tool_results_are_not_mistaken_for_questions(tmp_path):
    """A tool result arrives as a 'user' event; only the preamble marks a person."""
    _transcript(
        tmp_path, "s1",
        _line("user", CONTEXT + "check the queue"),
        _line("assistant", [{"type": "tool_use", "id": "t1",
                             "name": "mcp__aspen__list_directory", "input": {}}]),
        _line("user", [{"type": "tool_result", "tool_use_id": "t1", "content": "JOBID..."}]),
        _line("assistant", [{"type": "text", "text": "One job running."}]),
    )
    turns = T.read_all(tmp_path)[0]["turns"]
    assert len(turns) == 1
    assert turns[0]["reply"] == ["One job running."]
    assert [c["name"] for c in turns[0]["tools"]] == ["list_directory"]   # prefix stripped


def test_tool_arguments_are_kept_not_just_the_name(tmp_path):
    """The regression this exists for: Aspen was asked about Sam Tetef's jobs and
    ran `squeue -u samss` — a different person. "It checked the queue" and the
    actual command read identically until the arguments are on screen."""
    _transcript(
        tmp_path, "s1",
        _line("user", CONTEXT + "where are Sam Tetef's jobs?"),
        _line("assistant", [{"type": "tool_use", "id": "t1", "name": "Bash",
                             "input": {"command": "squeue -u samss",
                                       "description": "Show Sam's jobs"}}]),
    )
    call = T.read_all(tmp_path)[0]["turns"][0]["tools"][0]
    assert call["input"]["command"] == "squeue -u samss"
    assert T.call_line(call) == "$ squeue -u samss"      # description is narration


def test_call_line_renders_a_path_argument(tmp_path):
    call = {"name": "read_file", "input": {"path": "runs/orca.log", "section": "tail"}}
    assert T.call_line(call) == "read_file(path=runs/orca.log, section=tail)"


def test_call_line_survives_a_call_with_no_arguments(tmp_path):
    assert T.call_line({"name": "list_my_jobs", "input": {}}) == "list_my_jobs()"


def test_a_torn_final_line_does_not_lose_the_conversation(tmp_path):
    path = _transcript(tmp_path, "s1", *_exchange("q", "a"))
    with open(path, "a", encoding="utf-8") as fh:
        fh.write('{"type": "assist')                    # mid-write
    assert T.read_all(tmp_path)[0]["turns"][0]["reply"] == ["a"]


def test_title_is_the_first_thing_the_person_said(tmp_path):
    _transcript(tmp_path, "s1",
                *_exchange("Will you look at what was submitted by tetef01", "ok"),
                *_exchange("and on Friday?", "ok"))
    assert T.title_of(T.read_all(tmp_path)[0]).startswith("Will you look at what")


def test_index_sorts_newest_first(tmp_path):
    _transcript(tmp_path, "old", *_exchange("q", "a", ts="2026-08-01T10:00:00.000Z"))
    _transcript(tmp_path, "new", *_exchange("q", "a", ts="2026-08-30T10:00:00.000Z"))
    assert list(T.index(T.read_all(tmp_path))["session_id"]) == ["new", "old"]


# --------------------------------------------------------------------------- #
# The gate — the part that must not be wrong
# --------------------------------------------------------------------------- #
def _log_frame(*rows):
    return dataio._frame(list(rows))


def test_text_is_shown_when_the_log_kept_it(tmp_path):
    _transcript(tmp_path, "s1", *_exchange("did it converge?", "Yes."))
    log = _log_frame({"ts": "2026-08-31T18:02:46+00:00", "uid": "U08B", "outcome": "ok",
                      "session_id": "s1", "text": "did it converge?"})
    shown = T.visible_turns(T.read_all(tmp_path)[0], log)
    assert shown[0]["shown"] is True


def test_a_redacted_turn_stays_redacted_in_the_viewer(tmp_path):
    """The transcript has the text either way; the log's decision is what counts."""
    _transcript(tmp_path, "s1", *_exchange("something private", "an answer"))
    log = _log_frame({"ts": "2026-08-31T18:02:46+00:00", "uid": "U08B", "outcome": "ok",
                      "session_id": "s1", "text": None, "redacted": True})
    turn = T.visible_turns(T.read_all(tmp_path)[0], log)[0]
    assert turn["shown"] is False
    assert turn["why"] == "recorded without text"


def test_excluding_someone_hides_their_past_conversations(tmp_path):
    """Opting out takes effect backwards, not just from the next turn on."""
    _transcript(tmp_path, "s1", *_exchange("a question", "an answer"))
    log = _log_frame({"ts": "2026-08-31T18:02:46+00:00", "uid": "U08B", "outcome": "ok",
                      "session_id": "s1", "text": "a question"})
    turn = T.visible_turns(T.read_all(tmp_path)[0], log, excluded={"U08B"})[0]
    assert turn["shown"] is False
    assert turn["why"] == "person opted out"


def test_a_turn_with_no_log_row_is_shown_by_default(tmp_path):
    """Everything recorded before session_id was logged lands here — the whole
    existing backlog. Collection defaults to on, so a turn with no row to consult
    was recorded under whatever was in force then, not under a prohibition."""
    _transcript(tmp_path, "s1", *_exchange("q", "a"))
    turn = T.visible_turns(T.read_all(tmp_path)[0], _log_frame())[0]
    assert turn["shown"] is True and turn["unjoined"] is True


def test_strict_mode_withholds_turns_that_cannot_be_verified(tmp_path):
    """The other reading, for an operator who wants only checkable turns."""
    _transcript(tmp_path, "s1", *_exchange("q", "a"))
    turn = T.visible_turns(T.read_all(tmp_path)[0], _log_frame(),
                           show_unjoined=False)[0]
    assert turn["shown"] is False and turn["why"] == "no telemetry record"


def test_an_explicit_opt_out_is_honoured_even_in_the_default_open_mode(tmp_path):
    """Defaulting unjoined turns to visible must not weaken a real redaction."""
    _transcript(tmp_path, "s1", *_exchange("private", "answer"))
    log = _log_frame({"ts": "2026-08-31T18:02:46+00:00", "uid": "U08B", "outcome": "ok",
                      "session_id": "s1", "text": None, "redacted": True})
    assert T.visible_turns(T.read_all(tmp_path)[0], log)[0]["shown"] is False


def test_an_old_log_without_the_session_id_column_does_not_crash(tmp_path):
    _transcript(tmp_path, "s1", *_exchange("q", "a"))
    log = _log_frame({"ts": "2026-08-31T18:02:46+00:00", "uid": "U08B", "outcome": "ok"})
    assert T.visible_turns(T.read_all(tmp_path)[0], log)[0]["unjoined"] is True


def test_a_turn_does_not_adopt_a_distant_neighbours_row(tmp_path):
    """A turn the bot died mid-way through has no row; the guard stops it
    borrowing the consent decision made for a different question."""
    _transcript(tmp_path, "s1",
                *_exchange("first", "a", ts="2026-08-31T10:00:00.000Z"),
                *_exchange("second", "b", ts="2026-08-31T18:00:00.000Z"))
    log = _log_frame({"ts": "2026-08-31T10:00:01+00:00", "uid": "U08B",
                      "outcome": "ok", "session_id": "s1", "text": "first"})
    turns = T.visible_turns(T.read_all(tmp_path)[0], log)
    assert turns[0]["shown"] is True          # matched its own row
    assert turns[1]["unjoined"] is True       # eight hours away: not this one


def test_excluded_users_reads_the_state_file(tmp_path):
    state = tmp_path / "telemetry.json"
    state.write_text(json.dumps({"metrics": True, "excluded_users": ["U1", " U2 "]}))
    assert T.excluded_users(state) == {"U1", "U2"}


def test_excluded_users_survives_a_missing_or_broken_state_file(tmp_path):
    assert T.excluded_users(tmp_path / "absent.json") == set()
    broken = tmp_path / "broken.json"
    broken.write_text("{not json")
    assert T.excluded_users(broken) == set()
