"""
Characterization tests for conversation history — the per-thread store that the
refactor will turn into the SessionManager.

In Phase 1 these target the legacy ``_thread_key`` / ``_get_history`` /
``_append_history`` functions. After the refactor the conftest shim repoints the
same names at ``SessionManager`` / ``MessagesSession`` and these assertions stand.
"""


# --------------------------------------------------------------------------- #
# _thread_key
# --------------------------------------------------------------------------- #
def test_thread_key_prefers_thread_ts(sut):
    assert sut._thread_key({"channel": "C", "thread_ts": "1", "ts": "2"}) == "C:1"


def test_thread_key_falls_back_to_ts(sut):
    assert sut._thread_key({"channel": "C", "ts": "2"}) == "C:2"


def test_thread_key_handles_missing_fields(sut):
    assert sut._thread_key({}) == ":"


# --------------------------------------------------------------------------- #
# append / get round-trip
# --------------------------------------------------------------------------- #
def test_append_then_get_round_trip(sut):
    sut._append_history("C:1", "question", "answer")
    turns = sut._get_history("C:1")
    assert turns == [
        {"role": "user", "content": "question"},
        {"role": "assistant", "content": "answer"},
    ]


def test_get_history_missing_key(sut):
    assert sut._get_history("nokey") == []


# --------------------------------------------------------------------------- #
# trimming to CONTEXT_MAX_TURNS
# --------------------------------------------------------------------------- #
def test_history_trims_to_context_max_turns(sut):
    # Each append adds two entries (user + assistant); push well past the cap.
    n = sut.CONTEXT_MAX_TURNS  # appends -> 2*n entries before trimming
    for i in range(n):
        sut._append_history("C:1", f"q{i}", f"a{i}")

    turns = sut._get_history("C:1")
    assert len(turns) == sut.CONTEXT_MAX_TURNS
    # The most recent exchange must survive the trim.
    assert turns[-2:] == [
        {"role": "user", "content": f"q{n - 1}"},
        {"role": "assistant", "content": f"a{n - 1}"},
    ]


# --------------------------------------------------------------------------- #
# TTL expiry
# --------------------------------------------------------------------------- #
def test_history_expires_after_context_expiry(sut):
    sut._append_history("C:1", "q", "a")
    # Force the entry to look older than CONTEXT_EXPIRY.
    sut._histories["C:1"]["last_ts"] = 0.0

    assert sut._get_history("C:1") == []
    # Expired entries are evicted on read.
    assert "C:1" not in sut._histories
