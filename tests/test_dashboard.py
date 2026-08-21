"""
Tests for the dashboard's data layer (``dashboard/data.py``).

The module under test is a **second, independent reader of the turn log** — it
parses the JSONL itself rather than importing ``aspen.telemetry``, so that a
read-only web app doesn't drag in the bot's config and secrets. That decision is
sound and it has a cost: nothing fails loudly when the writer starts or stops
emitting a field. So the tests that matter most here are contract tests — drive
``telemetry.record`` and read the result back through ``read_turns`` — and the
first of them is the one that would have caught the real bug: ``denials`` is
written only on the turns that had one, which leaves the column full of ``NaN``,
and ``NaN`` is truthy, so ``x or []`` handed a float to a ``for`` loop.

The rest cover the derived columns the charts are drawn from (a wrong
``overhead_ms`` is a wrong conclusion about where the time goes, and nothing
about the page would look broken) and the fact that every helper survives the
shapes a real log actually takes: an empty one, a missing directory, a torn final
line, a turn from an unregistered sender.

``dashboard/app.py`` itself is a render script with no logic worth a harness; it
is exercised by ``./aspen-dashboard --demo``.
"""

import importlib.util
import json
from pathlib import Path

import pytest

pd = pytest.importorskip(
    "pandas", reason="dashboard tests need pandas — see requirements-dev.txt")


def _load_dashboard_data():
    """Load ``dashboard/data.py`` by path, under a name of its own.

    ``dashboard/`` is deliberately not a package (the launcher puts it on
    ``sys.path`` and imports ``data``), and ``data`` is far too generic a name to
    plant in ``sys.modules`` for a whole test session.
    """
    path = Path(__file__).resolve().parent.parent / "dashboard" / "data.py"
    spec = importlib.util.spec_from_file_location("aspen_dashboard_data", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


dataio = _load_dashboard_data()


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _log(tmp_path, *rows, name="20260801.jsonl", raw=()):
    """Write rows as a daily log and read them back the way the dashboard does.

    ``raw`` appends verbatim lines — blank, or torn mid-write — which is the only
    way to test that reading a live file being appended to is safe.
    """
    directory = tmp_path / "telemetry"
    directory.mkdir(exist_ok=True)
    with open(directory / name, "a", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row) + "\n")
        for line in raw:
            fh.write(line)
    return directory


def _turn(ts="2026-08-01T10:00:00+00:00", uid="U1", outcome="ok", **kwargs):
    """One record with the fields ``_frame`` requires, plus whatever's asked."""
    return {"ts": ts, "uid": uid, "outcome": outcome, **kwargs}


# --------------------------------------------------------------------------- #
# The contract with the writer — the reason this file exists
# --------------------------------------------------------------------------- #
def test_a_denial_is_found_among_turns_that_carry_no_denials_field(sut):
    """The regression. ``_flatten_meta`` keeps only fields that aren't None, so
    ``denials`` is absent from every turn that had none — and pandas fills those
    holes with float ``NaN``, which is truthy. ``turn.get("denials") or []`` let
    that float reach the ``for`` loop and the whole page died on a real log where
    79 of 80 turns had nothing denied."""
    sut.telemetry.record(uid="U1", outcome="ok", text="one")
    sut.telemetry.record(uid="U1", outcome="ok", text="two", meta={
        "denials": [{"tool": "Bash", "command": "squeue -u tetef01"}]})
    sut.telemetry.record(uid="U1", outcome="ok", text="three")

    df = dataio.read_turns(sut.TELEMETRY_DIR)
    # The shape that broke it: holes, not None — assert it, or the test passes
    # for the wrong reason the day pandas changes its mind about missing values.
    assert df["denials"].isna().sum() == 2
    assert not isinstance(df["denials"].iloc[0], list)

    out = dataio.denied_commands(df)
    assert list(out["command"]) == ["squeue -u tetef01"]
    assert list(out["times"]) == [1]


def test_reads_back_every_field_the_charts_are_drawn_from(sut):
    sut.telemetry.record(
        uid="U1", outcome="ok", text="where are my jobs?", channel="im",
        thread="C1:1.0", latency_ms=4200, first_output_ms=900, reply_chars=180,
        tools=["mcp__aspen__list_directory", "Bash"],
        meta={"result_subtype": "success", "num_turns": 3, "input_tokens": 12000,
              "output_tokens": 900, "agent_ms": 3800, "api_ms": 2500})

    row = dataio.read_turns(sut.TELEMETRY_DIR).iloc[0]
    assert row["tools"] == ["list_directory", "Bash"] and row["tool_count"] == 2
    assert row["tokens"] == 12900
    assert row["overhead_ms"] == 400          # 4200 latency - 3800 in the agent
    assert row["tool_ms"] == 1300             # 3800 in the agent - 2500 in the API
    assert not row["failed"]
    assert row["day"] == row["ts"].floor("D")


def test_a_registered_sender_shows_their_alias_an_unregistered_one_their_id(sut, env):
    """``who`` is what every chart groups by, so it must never be empty."""
    env.default_group()                        # sam, arun, priya
    sut.telemetry.record(uid="U01ARUN", outcome="ok", text="mine?")
    sut.telemetry.record(uid="U0NOBODY", outcome="ok", text="and mine?")

    assert list(dataio.read_turns(sut.TELEMETRY_DIR)["who"]) == ["arun", "U0NOBODY"]


def test_a_redacted_turn_still_counts_as_a_turn(sut):
    """Switching content off narrows a record; the volume series must not dip."""
    sut.telemetry.save({"metrics": True, "content": False}, actor="test")
    sut.telemetry.record(uid="U1", outcome="ok", text="unpublished result")

    df = dataio.read_turns(sut.TELEMETRY_DIR)
    assert len(df) == 1
    assert df["text"].isna().all()             # nothing to show in the question list
    assert df["chars"].iloc[0] == len("unpublished result")
    assert not df["failed"].iloc[0]


def test_every_recorded_outcome_is_classified(sut):
    """A new outcome in the writer must not quietly land in neither bucket.

    ``failed`` drives the failure panel *and* the median-latency headline (which
    reads ``~failed``), so an unclassified outcome skews both.
    """
    for outcome in sut.telemetry.OUTCOMES:
        sut.telemetry.record(uid="U1", outcome=outcome, text="x")

    df = dataio.read_turns(sut.TELEMETRY_DIR)
    failed = set(df.loc[df["failed"], "outcome"])
    assert failed == dataio.FAILURE_OUTCOMES
    # Not a failure: someone said hello, and a turn that answered.
    assert set(df.loc[~df["failed"], "outcome"]) == {"ok", "empty"}


# --------------------------------------------------------------------------- #
# Reading the log
# --------------------------------------------------------------------------- #
def test_turns_come_back_oldest_first_across_daily_files(tmp_path):
    _log(tmp_path, _turn(ts="2026-08-02T09:00:00+00:00", text="second"),
         name="20260802.jsonl")
    directory = _log(tmp_path, _turn(ts="2026-08-01T09:00:00+00:00", text="first"))

    assert list(dataio.read_turns(directory)["text"]) == ["first", "second"]


def test_a_torn_final_write_costs_one_line_not_the_page(tmp_path):
    """The bot appends while the dashboard reads. A half-written last line is
    normal, and must not take the other turns with it."""
    directory = _log(tmp_path, _turn(text="good"),
                     raw=["\n", '{"ts": "2026-08-01T10:01:00+00:00", "uid"\n'])

    assert list(dataio.read_turns(directory)["text"]) == ["good"]


def test_a_turn_with_an_unreadable_timestamp_is_dropped(tmp_path):
    """Every chart is a time series; a row with no place on the axis has none."""
    directory = _log(tmp_path, _turn(text="keep"), _turn(ts="not a date", text="drop"))

    assert list(dataio.read_turns(directory)["text"]) == ["keep"]


def test_a_log_directory_that_does_not_exist_yet_reads_as_empty(tmp_path):
    """Before the bot's first turn there is no directory at all."""
    assert dataio.read_turns(tmp_path / "never-written").empty


def test_junk_in_a_numeric_field_becomes_null_not_a_crash(tmp_path):
    directory = _log(tmp_path, _turn(latency_ms="n/a", input_tokens=None))
    row = dataio.read_turns(directory).iloc[0]

    assert row["latency_ms"] != row["latency_ms"]       # NaN
    assert row["tokens"] == 0                          # both sides missing -> 0


def test_a_tools_field_that_is_not_a_list_counts_as_no_tools(tmp_path):
    directory = _log(tmp_path, _turn(tools=None), _turn(tools="Bash"),
                     _turn(tools=["Bash", "Read"]))

    assert list(dataio.read_turns(directory)["tool_count"]) == [0, 0, 2]


# --------------------------------------------------------------------------- #
# Denied commands — the feature backlog
# --------------------------------------------------------------------------- #
def test_the_same_refusal_twice_ranks_above_a_one_off(tmp_path):
    directory = _log(
        tmp_path,
        _turn(denials=[{"tool": "Bash", "command": "squeue -u me"}]),
        _turn(denials=[{"tool": "Bash", "command": "sbatch run.slurm"},
                       {"tool": "Bash", "command": "squeue -u me"}]),
    )
    out = dataio.denied_commands(dataio.read_turns(directory))

    assert list(out["command"]) == ["squeue -u me", "sbatch run.slurm"]
    assert list(out["times"]) == [2, 1]


def test_a_malformed_denial_is_skipped_and_a_nameless_tool_is_marked(tmp_path):
    directory = _log(tmp_path, _turn(denials=[
        "scancel 1",                                   # not a dict; skip it
        {"command": "  tail -f orca.out  "},           # no tool name
    ]))
    out = dataio.denied_commands(dataio.read_turns(directory))

    assert list(out["tool"]) == ["?"]
    assert list(out["command"]) == ["tail -f orca.out"]


def test_no_denials_at_all_is_an_empty_table_with_columns(tmp_path):
    """The sidebar counts rows in this frame on every rerun, so the empty case
    has to be a frame with the right columns rather than a None."""
    directory = _log(tmp_path, _turn(text="nothing was refused"))
    out = dataio.denied_commands(dataio.read_turns(directory))

    assert out.empty and list(out.columns) == ["tool", "command", "times"]


# --------------------------------------------------------------------------- #
# Tool sequences, per-user
# --------------------------------------------------------------------------- #
def test_only_sequences_seen_more_than_once_are_worth_a_tool(tmp_path):
    """The point of the panel is repetition — a one-off chain is just a turn."""
    directory = _log(tmp_path,
                     _turn(tools=["read_file", "run_python_analysis", "attach_file"]),
                     _turn(tools=["read_file", "run_python_analysis", "attach_file",
                                  "write_metadata"]),
                     _turn(tools=["list_directory", "read_file"]))   # too short
    out = dataio.tool_sequences(dataio.read_turns(directory))

    assert list(out["sequence"]) == ["read_file → run_python_analysis → attach_file"]
    assert list(out["times"]) == [2]


def test_pairs_and_the_top_cap(tmp_path):
    directory = _log(tmp_path, _turn(tools=["a", "b", "c"]), _turn(tools=["a", "b", "c"]))

    pairs = dataio.tool_sequences(dataio.read_turns(directory), length=2)
    assert list(pairs["sequence"]) == ["a → b", "b → c"]
    assert len(dataio.tool_sequences(dataio.read_turns(directory), length=2, top=1)) == 1


def test_per_user_ranks_by_volume_and_costs_out_the_heavy_user(tmp_path):
    directory = _log(
        tmp_path,
        _turn(uid="U1", alias="arun", latency_ms=2000, input_tokens=10_000,
              output_tokens=500, tools=["read_file"]),
        _turn(uid="U1", alias="arun", latency_ms=6000, input_tokens=20_000,
              output_tokens=1500, outcome="timeout"),
        _turn(uid="U2", alias="priya", latency_ms=1000, input_tokens=5_000,
              output_tokens=100),
    )
    out = dataio.per_user(dataio.read_turns(directory))

    assert list(out.index) == ["arun", "priya"]
    assert out.loc["arun", "turns"] == 2
    assert out.loc["arun", "input tokens"] == 30_000
    assert out.loc["arun", "median answer (s)"] == 4.0
    assert out.loc["arun", "failure rate"] == 0.5      # the timeout
    assert out.loc["priya", "failure rate"] == 0.0


# --------------------------------------------------------------------------- #
# The shapes a real log takes
# --------------------------------------------------------------------------- #
def test_every_helper_survives_an_empty_log(tmp_path):
    """``app.py`` stops before the panels when the log is empty, but the frame is
    also filtered down to nothing whenever someone deselects every person in the
    sidebar — and then all three of these run on it."""
    empty = dataio.read_turns(tmp_path / "never-written")

    assert dataio.denied_commands(empty).empty
    assert dataio.tool_sequences(empty).empty
    assert dataio.per_user(empty).empty


def test_the_demo_log_exercises_every_panel(sut):
    """``--demo`` exists to be looked at before real traffic accumulates, so it
    has to reach the panels a thin real log would leave empty."""
    df = dataio.demo_turns(days=21, seed=7)

    assert not df.empty
    assert not dataio.denied_commands(df).empty
    assert not dataio.tool_sequences(df).empty
    assert not dataio.per_user(df).empty
    assert df["quota_utilization"].notna().any()
    assert df["failed"].any() and not df["failed"].all()


def test_the_demo_log_is_the_same_every_time_for_a_seed(sut):
    """A layout you're judging must not reshuffle under you on every rerun."""
    first, second = dataio.demo_turns(seed=3), dataio.demo_turns(seed=3)

    assert list(first["who"]) == list(second["who"])
    assert list(first["latency_ms"]) == list(second["latency_ms"])
    assert list(first["tools"]) == list(second["tools"])
    assert list(dataio.demo_turns(seed=4)["who"]) != list(first["who"])
