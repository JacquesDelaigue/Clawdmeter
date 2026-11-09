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


def test_served_reading_expires_after_failed_polls():
    """Freshness is checked when the reading is served, from a module-level
    stamp, so it survives a poller restart (round-2 finding)."""
    reading = {"acct": "week", "s": 0, "sr": -1, "w": 28, "wr": 100, "ok": True}
    with patch.object(d, "_api_success_ts", time.time()):
        assert d.served_payload(reading)["ok"] is True
    with patch.object(d, "_api_success_ts", time.time() - d.STALE_AFTER_SECONDS - 1):
        stale = d.served_payload(reading)
        assert (stale["ok"], stale["w"]) == (False, 28)
    assert reading["ok"] is True          # the stored reading is not mutated
    assert d.served_payload(None) is None


def test_poller_success_stamps_freshness_and_restart_keeps_expiry():
    calls = []

    async def scenario():
        stop = asyncio.Event()

        async def poll(selector=None):
            calls.append(1)
            if len(calls) == 1:
                return {"acct": "week", "s": 0, "sr": -1, "w": 28, "wr": 100, "ok": True}, False
            if len(calls) == 2:
                raise RuntimeError("poller crash")
            return None, False           # live token, transient failure (e.g. 429)

        with patch.object(d, "_force_poll", asyncio.Event()), \
             patch.object(d, "_payload_updated", asyncio.Event()), \
             patch.object(d, "_latest_payload", None), \
             patch.object(d, "_api_success_ts", 0.0), \
             patch.object(d, "poll_active", poll), \
             patch.object(d, "TICK", 0.01), \
             patch.object(d, "POLL_INTERVAL", 0), \
             patch.object(d, "POLL_RETRY_SECONDS", 0.02), \
             patch.object(d, "STALE_AFTER_SECONDS", 0.2):
            task = asyncio.create_task(d.poller(stop))
            await asyncio.sleep(0.05)
            with d._latest_lock:
                fresh = d.served_payload(d._latest_payload)
            await asyncio.sleep(0.4)
            with d._latest_lock:
                stale = d.served_payload(d._latest_payload)
            stop.set()
            await asyncio.wait_for(task, timeout=2)
        return fresh, stale

    fresh, stale = _run(scenario())
    assert len(calls) >= 3               # the crash happened and polling resumed
    assert fresh["ok"] is True and fresh["w"] == 28
    assert stale["ok"] is False and stale["w"] == 28


def test_empty_weekly_header_is_a_failed_poll():
    for bad in ("", "  "):
        assert _poll({"anthropic-ratelimit-unified-7d-utilization": bad}) is None, repr(bad)


def test_pro_account_rejects_bad_weekly_or_session_figures():
    reset = str(int(time.time()) + 3600)
    good = {
        "anthropic-ratelimit-unified-5h-utilization": "0.10",
        "anthropic-ratelimit-unified-5h-reset": reset,
        "anthropic-ratelimit-unified-7d-utilization": "0.28",
        "anthropic-ratelimit-unified-7d-reset": reset,
    }
    assert _poll(good)["acct"] == "pro"
    for name in ("anthropic-ratelimit-unified-5h-utilization",
                 "anthropic-ratelimit-unified-7d-utilization"):
        for bad in ("", "x", "-0.1", "nan", "inf", None):
            h = dict(good)
            if bad is None:
                del h[name]              # other 5h headers remain: still a 5h account
            else:
                h[name] = bad
            assert _poll(h) is None, (name, bad)


def test_infinite_reset_does_not_crash():
    p = _poll({
        "anthropic-ratelimit-unified-7d-utilization": "0.28",
        "anthropic-ratelimit-unified-7d-reset": "inf",
    })
    assert (p["acct"], p["w"], p["wr"]) == ("week", 28, 0)


def test_active_plan_failure_does_not_switch_to_another_plan():
    a, b = d.Path("/a"), d.Path("/b")
    readings = {a: {"acct": "week", "s": 0, "w": 10}, b: {"acct": "week", "s": 0, "w": 99}}

    async def fake_poll(token):
        r = readings[token]
        return None if r is None else dict(r)

    sel = d.PlanSelector()
    with patch.object(d, "read_config_dirs", lambda: [a, b]), \
         patch.object(d, "read_token_for", lambda p: p), \
         patch.object(d, "poll_api", fake_poll):
        first, _ = _run(d.poll_active(sel))
        assert first["w"] == 99          # b is active
        readings[b] = None               # b's poll fails; a still answers 10%
        second, dead = _run(d.poll_active(sel))
        assert (second, dead) == (None, False)


def test_partial_five_hour_headers_are_a_failed_poll():
    reset = str(int(time.time()) + 3600)
    p = _poll({
        "anthropic-ratelimit-unified-5h-status": "rejected",
        "anthropic-ratelimit-unified-5h-reset": reset,
        "anthropic-ratelimit-unified-7d-utilization": "0.10",
        "anthropic-ratelimit-unified-7d-reset": reset,
    })
    assert p is None


def test_freshness_is_stamped_only_when_the_payload_is_published():
    """A successful poll whose payload is never published (assembly crashes)
    must not re-trust the older reading still being served."""
    old = {"acct": "week", "s": 0, "sr": -1, "w": 10, "wr": 100, "ok": True}
    calls = []

    async def scenario():
        stop = asyncio.Event()

        async def poll(selector=None):
            calls.append(1)
            return {"acct": "week", "s": 0, "sr": -1, "w": 99, "wr": 100, "ok": True}, False

        def broken_state():
            raise RuntimeError("malformed hook state")

        stale_ts = time.time() - d.STALE_AFTER_SECONDS - 1
        with patch.object(d, "_force_poll", asyncio.Event()), \
             patch.object(d, "_payload_updated", asyncio.Event()), \
             patch.object(d, "_latest_payload", old), \
             patch.object(d, "_api_success_ts", stale_ts), \
             patch.object(d, "poll_active", poll), \
             patch.object(d, "read_working", broken_state), \
             patch.object(d, "TICK", 0.01), \
             patch.object(d, "POLL_INTERVAL", 0), \
             patch.object(d, "POLL_RETRY_SECONDS", 0.02):
            task = asyncio.create_task(d.poller(stop))
            await asyncio.sleep(0.2)
            with d._latest_lock:
                served = d.served_payload(d._latest_payload)
            stop.set()
            await asyncio.wait_for(task, timeout=2)
        return served

    served = _run(scenario())
    assert calls                         # polls succeeded
    assert (served["w"], served["ok"]) == (10, False)


def test_removed_active_plan_releases_the_choice():
    a, b = d.Path("/a"), d.Path("/b")
    readings = {a: {"acct": "week", "s": 0, "w": 10}, b: {"acct": "week", "s": 0, "w": 99}}
    dirs = [a, b]

    async def fake_poll(token):
        return dict(readings[token])

    sel = d.PlanSelector()
    with patch.object(d, "read_config_dirs", lambda: list(dirs)), \
         patch.object(d, "read_token_for", lambda p: p), \
         patch.object(d, "poll_api", fake_poll):
        assert _run(d.poll_active(sel))[0]["w"] == 99   # b active
        dirs.remove(b)                                    # b taken out of config
        assert _run(d.poll_active(sel))[0]["w"] == 10


def test_enterprise_non_finite_reset_does_not_crash():
    for bad in ("inf", "nan", "-inf"):
        p = _poll({
            "anthropic-ratelimit-unified-overage-utilization": "0.40",
            "anthropic-ratelimit-unified-overage-reset": bad,
        })
        assert (p["acct"], p["tp"], p["pd"]) == ("ent", 0, 30), bad
