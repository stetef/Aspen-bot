"""
Tests for the turn log — what Aspen records about how it is used.

Three things have to hold. The switches must be obeyed (including the two that
work without anyone touching them: the environment kill switch and the
self-closing content window). Turning content off must *narrow* a record rather
than drop it, so the volume and outcome series survive. And nothing here may ever
cost a turn — a broken log directory is a debug line, not a failed answer.
"""

import stat
from datetime import timedelta
from unittest.mock import MagicMock

import pytest


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _entries(sut) -> list[dict]:
    return sut.telemetry.read_all()


def _configure(sut, **settings) -> None:
    """Write the state file the way the CLI would."""
    sut.telemetry.save({**{"metrics": True, "content": True}, **settings}, actor="test")


def _record(sut, uid="U1", outcome="ok", **kwargs) -> None:
    sut.telemetry.record(uid=uid, outcome=outcome, **kwargs)


# --------------------------------------------------------------------------- #
# Recording
# --------------------------------------------------------------------------- #
def test_one_line_per_turn_with_the_fields_worth_analyzing(sut):
    _record(sut, text="where are my jobs?", channel="im", thread="C:1.0",
            latency_ms=4200, tools=["mcp__aspen__list_directory", "Bash"],
            reply_chars=180, attachments=1,
            meta={"result_subtype": "success", "num_turns": 3, "cost_usd": 0.04})
    _record(sut, uid="U2", text="plot the XAS spectra", tools=["mcp__aspen__run_python_analysis"])

    first, second = _entries(sut)
    assert first["uid"] == "U1" and first["outcome"] == "ok"
    assert first["text"] == "where are my jobs?"
    assert first["chars"] == len("where are my jobs?")
    assert first["latency_ms"] == 4200 and first["reply_chars"] == 180
    assert first["attachments"] == 1
    # The MCP prefix is noise in every single record; call order is not.
    assert first["tools"] == ["list_directory", "Bash"]
    assert first["result_subtype"] == "success" and first["cost_usd"] == 0.04
    assert second["tools"] == ["run_python_analysis"]


def test_turns_land_in_a_dated_file(sut):
    _record(sut, text="hi")
    files = sut.telemetry.log_files()
    assert len(files) == 1
    assert files[0].name == f"{sut.telemetry.today():%Y%m%d}.jsonl"


def test_the_log_is_private_on_a_shared_node(sut):
    """Other users on the login node must not be able to read what was asked."""
    _record(sut, text="unpublished result for the beamtime paper")
    log = sut.telemetry.log_files()[0]
    assert stat.S_IMODE(log.stat().st_mode) == 0o600
    assert stat.S_IMODE(log.parent.stat().st_mode) == 0o700


def test_long_text_is_truncated_but_the_true_length_is_kept(sut, monkeypatch):
    monkeypatch.setattr(sut, "TELEMETRY_MAX_TEXT", 50)
    _record(sut, text="x" * 5000)
    entry = _entries(sut)[0]
    assert len(entry["text"]) == 50
    assert entry["chars"] == 5000        # so a pasted traceback is still visible as one


# --------------------------------------------------------------------------- #
# The switches
# --------------------------------------------------------------------------- #
def test_metrics_off_records_nothing(sut):
    _configure(sut, metrics=False)
    _record(sut, text="anything")
    assert _entries(sut) == []


def test_content_off_keeps_the_turn_but_drops_the_text(sut):
    """The point of the split: you can stop reading questions without going blind
    to volume, latency, and failure rate."""
    _configure(sut, content=False)
    _record(sut, text="a question", latency_ms=900, tools=["mcp__aspen__read_file"])

    entry = _entries(sut)[0]
    assert entry["text"] is None and entry["redacted"] is True
    assert entry["chars"] == len("a question")
    assert entry["latency_ms"] == 900 and entry["tools"] == ["read_file"]


def test_excluding_one_person_leaves_everyone_else_alone(sut):
    _configure(sut, excluded_users=["U2"])
    _record(sut, uid="U1", text="kept")
    _record(sut, uid="U2", text="not kept")

    kept, excluded = _entries(sut)
    assert kept["text"] == "kept"
    assert excluded["text"] is None and excluded["redacted"] is True
    assert excluded["uid"] == "U2"       # still counted


def test_the_content_window_closes_by_itself(sut):
    """A collection window has to expire without anyone remembering to close it."""
    yesterday = (sut.telemetry.today() - timedelta(days=1)).isoformat()
    _configure(sut, content=True, content_until=yesterday)
    _record(sut, text="after the window")
    assert _entries(sut)[0]["text"] is None

    tomorrow = (sut.telemetry.today() + timedelta(days=1)).isoformat()
    _configure(sut, content=True, content_until=tomorrow)
    _record(sut, text="inside the window")
    assert _entries(sut)[1]["text"] == "inside the window"


def test_the_window_is_inclusive_of_its_last_day(sut):
    _configure(sut, content=True, content_until=sut.telemetry.today().isoformat())
    _record(sut, text="last day still counts")
    assert _entries(sut)[0]["text"] == "last day still counts"


def test_an_unparseable_window_stops_collection(sut):
    """A typo in the date must not silently mean 'collect forever'."""
    _configure(sut, content=True, content_until="next Tuesday")
    _record(sut, text="should not be kept")
    assert _entries(sut)[0]["text"] is None


def test_the_env_kill_switch_overrides_the_state_file(sut, monkeypatch):
    _configure(sut, metrics=True, content=True)
    monkeypatch.setattr(sut, "TELEMETRY_ENABLED", False)
    _record(sut, text="anything")
    assert _entries(sut) == []
    assert sut.telemetry.effective()["off_reason"]


def test_no_state_file_means_everything_on(sut):
    """Fresh install: the operator's .env is the only thing that has to be set."""
    assert not sut.TELEMETRY_STATE_FILE.exists()
    _record(sut, text="recorded by default")
    assert _entries(sut)[0]["text"] == "recorded by default"


def test_a_corrupt_state_file_falls_back_instead_of_crashing(sut):
    sut.TELEMETRY_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    sut.TELEMETRY_STATE_FILE.write_text("{ not json")
    sut.telemetry.invalidate()
    _record(sut, text="still recorded")
    assert _entries(sut)[0]["text"] == "still recorded"


def test_settings_survive_a_round_trip(sut):
    _configure(sut, content=True, content_until="2099-01-01", excluded_users=["U2", "U2", " "])
    sut.telemetry.invalidate()
    state = sut.telemetry.effective()
    assert state["content"] is True
    assert state["content_until"] == "2099-01-01"
    assert state["excluded_users"] == ["U2"]        # de-duplicated, blanks dropped
    assert state["updated_by"] == "test"
    assert stat.S_IMODE(sut.TELEMETRY_STATE_FILE.stat().st_mode) == 0o600


# --------------------------------------------------------------------------- #
# Never cost a turn
# --------------------------------------------------------------------------- #
def test_an_unwritable_log_location_is_survivable(sut, tmp_path):
    """A broken log directory must be a debug line, not a failed answer."""
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("i am a file")
    sut.TELEMETRY_DIR = blocker / "telemetry"
    _record(sut, text="the turn still succeeds")     # must not raise


def test_a_malformed_line_does_not_poison_the_read(sut):
    _record(sut, text="good")
    log = sut.telemetry.log_files()[0]
    with open(log, "a") as fh:
        fh.write("{ truncated write\n")
    _record(sut, text="also good")
    assert [e["text"] for e in _entries(sut)] == ["good", "also good"]


# --------------------------------------------------------------------------- #
# Pruning
# --------------------------------------------------------------------------- #
def test_prune_removes_only_logs_past_the_cutoff(sut):
    sut.TELEMETRY_DIR.mkdir(parents=True, exist_ok=True)
    old = (sut.telemetry.today() - timedelta(days=200)).strftime("%Y%m%d")
    recent = (sut.telemetry.today() - timedelta(days=5)).strftime("%Y%m%d")
    for stamp in (old, recent):
        (sut.TELEMETRY_DIR / f"{stamp}.jsonl").write_text("{}\n")
    (sut.TELEMETRY_DIR / "notes.txt").write_text("not mine")

    removed = sut.telemetry.prune(90)

    assert removed == [f"{old}.jsonl"]
    assert (sut.TELEMETRY_DIR / f"{recent}.jsonl").exists()
    assert (sut.TELEMETRY_DIR / "notes.txt").exists()      # untouched


# --------------------------------------------------------------------------- #
# The wiring: a real turn through _handle_event
# --------------------------------------------------------------------------- #
def _run_turn(sut, monkeypatch, say, uid="U1", text="how many jobs are queued?",
              reply="12 running", tools=(), meta=None, boom=None):
    async def _fake_handle(key, user_message, context):
        for name in tools:
            context["on_progress"](name, {"path": "x"})
        if boom is not None:
            raise boom
        if meta is not None:
            context["result_meta"] = meta
        return reply, []

    monkeypatch.setattr(sut.MANAGER, "handle", _fake_handle)
    sut._handle_event(
        {"user": uid, "text": text, "channel": "C", "ts": "1.0", "channel_type": "im"},
        say, MagicMock(), strip_mention=False,
    )


def test_a_real_turn_is_recorded_end_to_end(sut, monkeypatch, say):
    _run_turn(sut, monkeypatch, say,
              tools=["mcp__aspen__list_directory", "Bash"],
              meta={"result_subtype": "success", "num_turns": 2, "cost_usd": 0.02})

    entry = _entries(sut)[0]
    assert entry["outcome"] == "ok" and entry["uid"] == "U1"
    assert entry["text"] == "how many jobs are queued?"
    assert entry["tools"] == ["list_directory", "Bash"]
    assert entry["channel"] == "im" and entry["thread"] == "C:1.0"
    assert entry["reply_chars"] == len("12 running")
    assert entry["result_subtype"] == "success" and entry["cost_usd"] == 0.02
    assert entry["latency_ms"] >= 0


def test_recording_a_turn_does_not_disturb_the_progress_indicator(sut, monkeypatch, say):
    """The tool list piggybacks on the progress callback; the status must still fire."""
    seen = []
    monkeypatch.setattr(sut, "_start_typing_status",
                        lambda *a, **k: MagicMock(update=lambda n, i: seen.append(n)))
    _run_turn(sut, monkeypatch, say, tools=["mcp__aspen__read_file"])

    assert seen == ["mcp__aspen__read_file"]
    assert _entries(sut)[0]["tools"] == ["read_file"]


def test_a_failed_turn_is_recorded_as_such(sut, monkeypatch, say):
    _run_turn(sut, monkeypatch, say, boom=RuntimeError("backend went away"))
    entry = _entries(sut)[0]
    assert entry["outcome"] == "error"
    assert entry["text"] == "how many jobs are queued?"   # what they were asking when it broke


def test_refusals_are_recorded_because_they_are_demand_too(sut, monkeypatch, say):
    """Rate limits and allowlist gates are the tasks Aspen is currently turning
    away — the most actionable rows in the log."""
    monkeypatch.setattr(sut, "RATE_LIMIT_REQUESTS", 0)
    _run_turn(sut, monkeypatch, say, text="analyse this")
    assert _entries(sut)[0]["outcome"] == "rate_limited"


def test_an_unapproved_sender_is_counted_but_never_quoted(sut, monkeypatch, say):
    """Someone not on the allowlist hasn't agreed to anything, so their text is
    never recorded — whatever the content switch says."""
    _run_turn(sut, monkeypatch, say, uid="U9", text="who are you?")
    entry = _entries(sut)[0]
    assert entry["outcome"] == "not_authorized" and entry["uid"] == "U9"
    assert entry["text"] is None and entry["redacted"] is True


def test_an_empty_mention_is_recorded_without_a_turn(sut, monkeypatch, say):
    _run_turn(sut, monkeypatch, say, text="   ")
    entry = _entries(sut)[0]
    assert entry["outcome"] == "empty" and entry["tools"] == []


# --------------------------------------------------------------------------- #
# The admin CLI
# --------------------------------------------------------------------------- #
def _cli(sut, *argv, capsys=None) -> str:
    from aspen import users_cli
    assert users_cli.main(list(argv)) == 0
    return capsys.readouterr().out if capsys else ""


def test_cli_content_window_is_written_as_a_date(sut, capsys):
    _cli(sut, "telemetry", "content", "on", "--days", "30", capsys=capsys)
    expected = (sut.telemetry.today() + timedelta(days=30)).isoformat()
    assert sut.telemetry.effective()["content_until"] == expected


def test_cli_refuses_a_window_that_already_closed(sut):
    from aspen import users_cli
    yesterday = (sut.telemetry.today() - timedelta(days=1)).isoformat()
    assert users_cli.main(["telemetry", "content", "on", "--until", yesterday]) == 1


def test_cli_off_then_on_round_trips(sut, capsys):
    _cli(sut, "telemetry", "off", capsys=capsys)
    assert sut.telemetry.effective()["metrics"] is False
    _cli(sut, "telemetry", "on", capsys=capsys)
    assert sut.telemetry.effective()["metrics"] is True


def test_cli_exclude_and_include_round_trip(sut, capsys):
    _cli(sut, "telemetry", "exclude", "U2", capsys=capsys)
    assert sut.telemetry.effective()["excluded_users"] == ["U2"]
    _cli(sut, "telemetry", "include", "U2", capsys=capsys)
    assert sut.telemetry.effective()["excluded_users"] == []


def test_cli_status_says_what_is_collected_and_until_when(sut, capsys):
    _cli(sut, "telemetry", "content", "on", "--until", "2099-06-01", capsys=capsys)
    _cli(sut, "telemetry", "exclude", "U2", capsys=capsys)
    out = _cli(sut, "telemetry", "status", capsys=capsys)

    assert "metrics        on" in out
    assert "2099-06-01" in out
    assert "U2" in out
    assert str(sut.TELEMETRY_DIR) in out


def test_cli_status_explains_why_content_is_off(sut, capsys):
    _cli(sut, "telemetry", "content", "off", capsys=capsys)
    assert "switched off" in _cli(sut, "telemetry", "status", capsys=capsys)


# --------------------------------------------------------------------------- #
# The startup guard
# --------------------------------------------------------------------------- #
def test_startup_refuses_a_turn_log_inside_the_workspace(sut, monkeypatch):
    """A record of what the agent did is worth little if the agent can rewrite it,
    and a switch it can reach is a switch it can turn off."""
    import aspen.main  # noqa: F401

    monkeypatch.setattr(sut, "WORKSPACE_ROOT", sut.TELEMETRY_DIR.parent)
    with pytest.raises(SystemExit, match="sandbox-writable"):
        sut._check_state_locations()


def test_startup_refuses_the_switch_inside_a_sandbox_write_path(sut, monkeypatch):
    import aspen.main  # noqa: F401
    from aspen import config

    monkeypatch.setattr(config, "SANDBOX_WRITE_PATHS", [str(sut.TELEMETRY_STATE_FILE.parent)])
    with pytest.raises(SystemExit, match="sandbox-writable"):
        sut._check_state_locations()
