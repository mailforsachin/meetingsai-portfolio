"""Regression tests for the two-signal liveness decision.

The browser implements this in `meeting.html` (`livenessTick`).  We mirror
the decision here so a change to the logic is caught by CI before it
reaches a live meeting.  If the logic changes, both sides must change
together.
"""
from __future__ import annotations

import pytest


NO_INPUT_THRESHOLD = 90
WS_STALE_THRESHOLD = 30


def decide(
    server_age: float,       # seconds since last recorder input (or +inf)
    ws_status: str,          # connecting | connected | reconnecting | disconnected
    ws_age: float,           # seconds since last WS message (or +inf)
    meeting_status: str = "active",
) -> str:
    """Return one of: hidden | stale | reconnecting | no_input | both_down."""
    if meeting_status not in ("active", "stopping"):
        return "hidden"
    if server_age < NO_INPUT_THRESHOLD and ws_status == "connected" and ws_age < WS_STALE_THRESHOLD:
        return "hidden"
    if server_age < NO_INPUT_THRESHOLD and ws_status == "connected" and ws_age >= WS_STALE_THRESHOLD:
        return "stale"
    if server_age < NO_INPUT_THRESHOLD and ws_status != "connected":
        return "reconnecting"
    if server_age >= NO_INPUT_THRESHOLD and ws_status == "connected":
        return "no_input"
    return "both_down"


def test_healthy_hides_banner():
    assert decide(2.0, "connected", 1.0) == "hidden"


def test_server_stale_but_ws_healthy_is_stale_not_no_input():
    # This is the exact case that caused the production bug:
    # recorder keeps sending (server_age small), WS is connected,
    # but the browser has not received a frame in > 30 s.
    result = decide(2.0, "connected", 45.0)
    assert result == "stale"
    assert result != "no_input"


def test_ws_down_but_server_receiving_is_reconnecting():
    assert decide(2.0, "reconnecting", 999.0) == "reconnecting"
    assert decide(2.0, "disconnected", 999.0) == "reconnecting"


def test_server_stopped_and_ws_healthy_is_no_input():
    assert decide(120.0, "connected", 1.0) == "no_input"


def test_both_down():
    assert decide(120.0, "disconnected", 999.0) == "both_down"


def test_inactive_meeting_hides_banner():
    assert decide(120.0, "disconnected", 999.0, meeting_status="completed") == "hidden"
    assert decide(2.0, "connected", 1.0, meeting_status="created") == "hidden"


def test_exact_thresholds():
    # At 90 s server age, we flip into no_input for a connected WS.
    assert decide(89.9, "connected", 1.0) == "hidden"
    assert decide(90.0, "connected", 1.0) == "no_input"
    # At 30 s WS age, we flip into stale for a healthy server.
    assert decide(2.0, "connected", 29.9) == "hidden"
    assert decide(2.0, "connected", 30.0) == "stale"
