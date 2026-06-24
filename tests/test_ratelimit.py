"""Characterization tests for per-user rate limiting and the in-flight flag."""

import time


def test_first_request_passes_and_marks_active(sut):
    assert sut._check_rate_limit("U1") is None
    assert sut._user_active["U1"] is True


def test_reentrant_request_is_blocked_while_active(sut):
    assert sut._check_rate_limit("U1") is None
    msg = sut._check_rate_limit("U1")
    assert msg is not None
    assert "still working on your previous request" in msg


def test_release_user_clears_active(sut):
    sut._check_rate_limit("U1")
    sut._release_user("U1")
    assert sut._user_active["U1"] is False


def test_rate_limit_cap(sut):
    # Release between calls so the in-flight flag never short-circuits the count.
    for _ in range(sut.RATE_LIMIT_REQUESTS):
        assert sut._check_rate_limit("U2") is None
        sut._release_user("U2")

    msg = sut._check_rate_limit("U2")
    assert msg is not None
    assert f"You've sent {sut.RATE_LIMIT_REQUESTS} requests" in msg


def test_old_timestamps_fall_out_of_window(sut):
    # A timestamp older than the window must be evicted, freeing the user.
    sut._rate_data["U3"] = [time.time() - sut.RATE_LIMIT_WINDOW - 1]
    assert sut._check_rate_limit("U3") is None
    # The stale entry is gone; only the just-recorded request remains.
    assert len(sut._rate_data["U3"]) == 1
