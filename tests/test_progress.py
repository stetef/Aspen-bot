"""
Tests for the live "what Aspen is working on" indicator.

Two halves: the pure ``_progress_phrase`` mapping (tool call -> wording), and the
``_Progress`` transport, which prefers ``assistant.threads.setStatus`` and falls
back to editing a posted message when the thread isn't an assistant thread.
"""

import time
from unittest.mock import MagicMock

import pytest


def _phrase(sut, name, inp=None):
    from aspen.slack_app import _progress_phrase
    return _progress_phrase(name, inp or {})


def test_phrases_name_the_actual_work(sut):
    assert "orca.out" in _phrase(sut, "mcp__aspen__read_file", {"path": "runs/orca.out"})
    assert "browsing" in _phrase(sut, "mcp__aspen__list_directory", {"path": "runs"})
    assert "spin" in _phrase(sut, "mcp__aspen__search_files", {"pattern": "spin"})
    assert _phrase(sut, "mcp__aspen__run_python_analysis") == "running analysis code…"
    assert _phrase(sut, "mcp__aspen__write_metadata") == "updating metadata.md…"
    # Bash reports the command, which is what the user cares about on the cluster.
    assert _phrase(sut, "Bash", {"command": "squeue -u alice"}) == "running squeue…"


def test_phrases_are_gerunds_so_they_read_after_is(sut):
    """Slack renders the status as "Aspen <status>", so every phrase must complete
    "Aspen is …". A phrase that doesn't reads as broken English in the UI."""
    from aspen import tools

    for spec in tools.TOOL_SPECS:
        phrase = _phrase(sut, f"mcp__aspen__{spec['name']}", {"path": "p", "pattern": "q"})
        assert phrase and phrase[0].islower() and phrase.endswith("…")


def test_unknown_tool_falls_back_instead_of_breaking(sut):
    # A tool added later must never produce an empty or malformed indicator.
    assert _phrase(sut, "SomeFutureTool", {"weird": 1}) == "working…"
    assert _phrase(sut, "Bash", {}) == "checking the cluster…"


def test_long_paths_are_shortened_to_one_line(sut):
    from aspen.slack_app import _PROGRESS_MAX_DETAIL

    deep = "/very/long/path/" + "nested/" * 20 + "results.dat"
    phrase = _phrase(sut, "mcp__aspen__read_file", {"path": deep})
    assert "results.dat" in phrase
    assert len(phrase) < _PROGRESS_MAX_DETAIL + 20


def _status_calls(client):
    return [c.kwargs["status"] for c in client.assistant_threads_setStatus.call_args_list]


def test_tool_calls_drive_the_thread_status(sut):
    from aspen.slack_app import _Progress

    client = MagicMock()
    p = _Progress(client, "C", "1.0", MagicMock())
    try:
        p.update("mcp__aspen__read_file", {"path": "runs/orca.out"})
        deadline = time.time() + 5
        while time.time() < deadline and not any("orca.out" in s for s in _status_calls(client)):
            time.sleep(0.05)
    finally:
        p.stop()

    statuses = _status_calls(client)
    assert statuses[0] == sut._STATUS_TEXT                  # opens with "is typing…"
    assert "is reading runs/orca.out…" in statuses          # then names the real work
    assert statuses[-1] == ""                               # cleared before the reply


def test_status_bursts_are_coalesced(sut, monkeypatch):
    """Many tool calls in quick succession must not become many Slack calls —
    only the latest phrase is pushed once the floor between pushes has passed."""
    from aspen import slack_app

    monkeypatch.setattr(slack_app, "_STATUS_MIN_INTERVAL", 0.3)
    client = MagicMock()
    p = slack_app._Progress(client, "C", "1.0", MagicMock())
    try:
        for i in range(20):
            p.update("mcp__aspen__read_file", {"path": f"f{i}.out"})
        time.sleep(0.6)
    finally:
        p.stop()

    pushes = [s for s in _status_calls(client) if s.startswith("is reading")]
    assert len(pushes) < 20         # coalesced, not one call per tool use
    assert pushes[-1] == "is reading f19.out…"   # and the latest state wins


def test_falls_back_to_editing_a_message_off_assistant_threads(sut):
    """A channel @-mention isn't an assistant thread, so setStatus raises. Progress
    then lives in the posted message, edited in place rather than left static."""
    from aspen.slack_app import _Progress

    client = MagicMock()
    client.assistant_threads_setStatus.side_effect = Exception("not an assistant thread")
    say = MagicMock(return_value={"ts": "9.9"})

    p = _Progress(client, "C", "1.0", say)
    try:
        p.update("Bash", {"command": "squeue -u alice"})
        deadline = time.time() + 5
        while time.time() < deadline and not client.chat_update.called:
            time.sleep(0.05)
    finally:
        p.stop()

    say.assert_called_once_with(text="_Thinking…_", thread_ts="1.0")
    edit = client.chat_update.call_args.kwargs
    assert edit["ts"] == "9.9" and edit["channel"] == "C"
    assert edit["text"] == "_Running squeue…_"     # standalone: no "is " prefix
    # The placeholder is cleaned up so it doesn't linger above the answer.
    client.chat_delete.assert_called_once_with(channel="C", ts="9.9")


def test_fallback_without_a_message_ts_stays_quiet(sut):
    """If the placeholder can't be posted (or gives no ts) there's nothing to edit;
    the turn must still run rather than crash on updates."""
    from aspen.slack_app import _Progress

    client = MagicMock()
    client.assistant_threads_setStatus.side_effect = Exception("nope")
    p = _Progress(client, "C", "1.0", MagicMock(return_value=None))
    p.update("mcp__aspen__read_file", {"path": "x"})
    p.stop()

    assert not client.chat_update.called
    assert not client.chat_delete.called


def test_handle_event_passes_a_progress_hook_to_the_agent(sut, say, monkeypatch):
    """The wiring that matters: the agent gets a callback it can report tools on."""
    seen = {}

    async def _fake_handle(key, user_message, context):
        seen["on_progress"] = context.get("on_progress")
        context["on_progress"]("mcp__aspen__read_file", {"path": "runs/orca.out"})
        return "done", []

    monkeypatch.setattr(sut.MANAGER, "handle", _fake_handle)
    client = MagicMock()
    sut._handle_event(
        {"user": "U1", "text": "hi", "channel": "C", "ts": "1.0"},
        say, client, strip_mention=False,
    )

    assert callable(seen["on_progress"])
    assert "done" in say.texts
