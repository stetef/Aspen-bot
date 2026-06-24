"""Tests for the per-thread session key.

Conversation context is retained inside each warm SDK session (the SessionManager
keeps one per thread), so there is no separate history store to test here.
"""


def test_thread_key_prefers_thread_ts(sut):
    assert sut._thread_key({"channel": "C", "thread_ts": "1", "ts": "2"}) == "C:1"


def test_thread_key_falls_back_to_ts(sut):
    assert sut._thread_key({"channel": "C", "ts": "2"}) == "C:2"


def test_thread_key_handles_missing_fields(sut):
    assert sut._thread_key({}) == ":"
