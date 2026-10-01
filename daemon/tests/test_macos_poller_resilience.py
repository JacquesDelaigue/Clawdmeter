#!/usr/bin/env python3
"""Poller and watchdog resilience for the macOS daemon (2026-10-01 outage).

An InterruptedError (EINTR) raised while httpx built its SSL context escaped
poll_api, killed the poller task, and left /usage answering stale or 503
until the process restarted; the watchdog task has died the same way
(FileNotFoundError). These tests pin the four fixes: poll_api treats OSError
as a failed poll, the supervisor restarts the poller loop and the watchdog
loop after any exception, and a failed poll waits POLL_RETRY_SECONDS before
the next try.

Run: python -m pytest daemon/tests/test_macos_poller_resilience.py -x -q
"""
import asyncio
from unittest.mock import patch

import daemon.claude_usage_daemon as d


def _run(coro):
    """Run on a private loop. asyncio.run() would leave no current event loop,
    which breaks later tests that still call asyncio.get_event_loop()."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def test_poll_api_returns_none_on_interrupted_error():
    class Boom:
        def __init__(self, *a, **k):
            raise InterruptedError(4, "Interrupted system call")

    with patch.object(d.httpx, "AsyncClient", Boom):
        assert _run(d.poll_api("token")) is None


def test_poller_supervisor_restarts_after_crash():
    calls = []

    async def scenario():
        stop = asyncio.Event()

        async def fake_loop(stop_event):
            calls.append(1)
            if len(calls) == 1:
                raise RuntimeError("stray")
            stop_event.set()

        with patch.object(d, "_poller_loop", fake_loop), patch.object(d, "POLL_RETRY_SECONDS", 0.01):
            await asyncio.wait_for(d.poller(stop), timeout=2)

    _run(scenario())
    assert len(calls) == 2


def test_failed_poll_waits_retry_seconds_before_next_try():
    polls = []

    async def scenario():
        stop = asyncio.Event()

        async def failing_poll(token):
            polls.append(1)
            return None

        with patch.object(d, "_force_poll", asyncio.Event()), \
             patch.object(d, "poll_api", failing_poll), \
             patch.object(d, "read_token", lambda: "t"), \
             patch.object(d, "TICK", 0.01), \
             patch.object(d, "POLL_RETRY_SECONDS", 0.5):
            task = asyncio.create_task(d._poller_loop(stop))
            await asyncio.sleep(0.2)
            stop.set()
            await asyncio.wait_for(task, timeout=2)

    _run(scenario())
    assert len(polls) == 1


def test_watchdog_supervisor_restarts_after_crash():
    calls = []

    async def scenario():
        stop = asyncio.Event()

        async def fake_loop(stop_event):
            calls.append(1)
            if len(calls) == 1:
                raise FileNotFoundError(2, "No such file or directory")
            stop_event.set()

        with patch.object(d, "_watchdog_loop", fake_loop), patch.object(d, "POLL_RETRY_SECONDS", 0.01):
            await asyncio.wait_for(d.watchdog(stop), timeout=2)

    _run(scenario())
    assert len(calls) == 2
