#!/usr/bin/env python3
"""Account-kind detection in the macOS daemon's poll_api (2026-11-09).

From 2026-10-31 the API stopped sending the 5-hour headers for a Max account
and sent only the 7-day window. poll_api keyed "pro" on the 5-hour header, so
it fell to the Enterprise branch and reported acct "ent" with s and w at 0.
These tests pin the three branches: 5h present -> "pro"; only 7d -> "week"
(upstream's wire format, s 0 and sr -1); neither -> "ent".

Run: python -m pytest daemon/tests/test_macos_week_format.py -x -q
"""
import asyncio
import time
from unittest.mock import patch

import daemon.claude_usage_daemon as d


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class _Resp:
    def __init__(self, headers):
        self.status_code = 200
        self.headers = headers
        self.text = ""


def _client(headers):
    class Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, *a, **k):
            return _Resp(headers)

    return Client


def _poll(headers):
    with patch.object(d.httpx, "AsyncClient", _client(headers)):
        return _run(d.poll_api("token"))


def test_weekly_only_headers_give_week_account():
    reset = str(int(time.time()) + 3 * 86400)
    p = _poll({
        "anthropic-ratelimit-unified-7d-utilization": "0.28",
        "anthropic-ratelimit-unified-7d-reset": reset,
        "anthropic-ratelimit-unified-7d-status": "allowed",
        "anthropic-ratelimit-unified-status": "allowed",
        "anthropic-ratelimit-unified-overage-status": "rejected",
    })
    assert p["acct"] == "week"
    assert (p["s"], p["sr"]) == (0, -1)
    assert p["w"] == 28
    assert 4310 <= p["wr"] <= 4320
    assert p["st"] == "allowed"
    assert p["ok"] is True


def test_five_hour_headers_still_give_pro():
    reset = str(int(time.time()) + 3600)
    p = _poll({
        "anthropic-ratelimit-unified-5h-utilization": "0.10",
        "anthropic-ratelimit-unified-5h-reset": reset,
        "anthropic-ratelimit-unified-5h-status": "allowed",
        "anthropic-ratelimit-unified-7d-utilization": "0.28",
        "anthropic-ratelimit-unified-7d-reset": reset,
    })
    assert p["acct"] == "pro"
    assert (p["s"], p["w"]) == (10, 28)


def test_no_window_headers_still_give_ent():
    p = _poll({
        "anthropic-ratelimit-unified-overage-utilization": "0.40",
        "anthropic-ratelimit-unified-status": "allowed",
    })
    assert p["acct"] == "ent"
    assert p["s"] == 40


def test_unreadable_weekly_utilization_is_a_failed_poll():
    reset = str(int(time.time()) + 3 * 86400)
    for bad in ("not-a-number", "nan", "inf", "-0.1"):
        p = _poll({
            "anthropic-ratelimit-unified-7d-utilization": bad,
            "anthropic-ratelimit-unified-7d-reset": reset,
        })
        assert p is None, bad


def test_zero_weekly_utilization_is_a_real_reading():
    reset = str(int(time.time()) + 3 * 86400)
    p = _poll({
        "anthropic-ratelimit-unified-7d-utilization": "0",
        "anthropic-ratelimit-unified-7d-reset": reset,
    })
    assert (p["acct"], p["w"], p["ok"]) == ("week", 0, True)


def test_week_accounts_are_chosen_by_weekly_use():
    a, b = d.Path("/a"), d.Path("/b")
    readings = {a: {"acct": "week", "s": 0, "w": 10}, b: {"acct": "week", "s": 0, "w": 99}}

    async def fake_poll(token):
        return dict(readings[token])

    sel = d.PlanSelector()
    with patch.object(d, "read_config_dirs", lambda: [a, b]), \
         patch.object(d, "read_token_for", lambda p: p), \
         patch.object(d, "poll_api", fake_poll):
        first, _ = _run(d.poll_active(sel))
        assert first["w"] == 99          # startup: highest weekly % wins
        readings[a]["w"] = 11            # a's weekly use rises -> a is active
        second, _ = _run(d.poll_active(sel))
        assert second["w"] == 11


def test_failed_polls_expire_a_trusted_reading():
    calls = []

    async def scenario():
        stop = asyncio.Event()

        async def poll(selector=None):
            calls.append(1)
            if len(calls) == 1:
                return {"acct": "week", "s": 0, "sr": -1, "w": 28, "wr": 100, "ok": True}, False
            return None, False           # live token, transient failure (e.g. 429)

        with patch.object(d, "_force_poll", asyncio.Event()), \
             patch.object(d, "_payload_updated", asyncio.Event()), \
             patch.object(d, "poll_active", poll), \
             patch.object(d, "TICK", 0.01), \
             patch.object(d, "POLL_INTERVAL", 0), \
             patch.object(d, "POLL_RETRY_SECONDS", 0.02), \
             patch.object(d, "STALE_AFTER_SECONDS", 0.1):
            task = asyncio.create_task(d._poller_loop(stop))
            await asyncio.sleep(0.05)
            with d._latest_lock:
                fresh = dict(d._latest_payload)
            await asyncio.sleep(0.3)
            with d._latest_lock:
                stale = dict(d._latest_payload)
            stop.set()
            await asyncio.wait_for(task, timeout=2)
        return fresh, stale

    fresh, stale = _run(scenario())
    assert fresh["ok"] is True and fresh["w"] == 28
    assert stale["ok"] is False and stale["w"] == 28
