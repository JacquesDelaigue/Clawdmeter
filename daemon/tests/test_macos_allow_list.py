#!/usr/bin/env python3
"""/usage answers loopback and CLAWDMETER_ALLOW_IPS only (2026-10-08 lockdown).

The HTTP endpoint binds 0.0.0.0 so the meter's Wi-Fi fallback can reach it, but
it used to answer anyone on the LAN. These tests pin the allow-list: parsing,
loopback (v4, v6, IPv4-mapped) always allowed, empty list = loopback only, an
empty 403 for everyone else, and a refusal log line rate-limited per IP.

Run: python -m pytest daemon/tests/test_macos_allow_list.py -x -q
"""
import json
import threading
import urllib.error
import urllib.request
from unittest.mock import patch

import pytest

import daemon.claude_usage_daemon as d

LAN_IP = "192.168.1.50"


@pytest.fixture(autouse=True)
def clean_state():
    """Each test starts with an empty allow-list and no refusal history, whatever
    CLAWDMETER_ALLOW_IPS happens to be in the environment."""
    d._refusal_logged_at.clear()
    with patch.object(d, "HTTP_ALLOW_IPS", frozenset()), patch.object(d, "HTTP_TOKEN", ""):
        yield
    d._refusal_logged_at.clear()


def _refusals(capsys) -> list[str]:
    return [l for l in capsys.readouterr().out.splitlines() if "refused /usage from" in l]


# --- parse_allow_ips ---------------------------------------------------------

def test_parse_empty():
    assert d.parse_allow_ips("") == frozenset()
    assert d.parse_allow_ips(" , ,") == frozenset()


def test_parse_tolerates_spaces():
    assert d.parse_allow_ips(" 192.168.1.50 ,10.0.0.7,  ") == frozenset({"192.168.1.50", "10.0.0.7"})


def test_parse_ignores_and_logs_invalid_entry(capsys):
    got = d.parse_allow_ips("192.168.1.50, not-an-ip, 192.168.1.0/24")
    assert got == frozenset({"192.168.1.50"})
    out = capsys.readouterr().out
    assert "'not-an-ip'" in out and "'192.168.1.0/24'" in out


def test_parse_ipv6_and_mapped_are_normalised():
    got = d.parse_allow_ips("FE80:0:0::1, ::ffff:192.168.1.50")
    assert got == frozenset({"fe80::1", "192.168.1.50"})


# --- client_allowed ----------------------------------------------------------

@pytest.mark.parametrize("ip", ["127.0.0.1", "127.8.9.10", "::1", "::ffff:127.0.0.1"])
def test_loopback_allowed_with_empty_list(ip):
    assert d.client_allowed(ip, frozenset())


def test_lan_ip_refused_with_empty_list():
    assert not d.client_allowed(LAN_IP, frozenset())


def test_allowed_ip_accepted():
    allow = d.parse_allow_ips(LAN_IP)
    assert d.client_allowed(LAN_IP, allow)
    assert not d.client_allowed("192.168.1.51", allow)


def test_mapped_form_of_allowed_ip_accepted():
    assert d.client_allowed("::ffff:" + LAN_IP, d.parse_allow_ips(LAN_IP))


def test_garbage_client_address_refused():
    assert not d.client_allowed("", frozenset({""}))
    assert not d.client_allowed("0.0.0.0/0", frozenset({"0.0.0.0/0"}))


# --- real server -------------------------------------------------------------

class _FakePeerServer(d.ThreadingHTTPServer):
    """Hands the handler a chosen client address instead of the real (loopback) one."""
    fake_addr = None

    def finish_request(self, request, client_address):
        self.RequestHandlerClass(request, self.fake_addr or client_address, self)


@pytest.fixture
def server():
    srv = _FakePeerServer(("127.0.0.1", 0), d._UsageHandler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    with d._latest_lock:
        saved = d._latest_payload
        d._latest_payload = {"s": 42}
    try:
        yield srv
    finally:
        srv.shutdown()
        srv.server_close()
        with d._latest_lock:
            d._latest_payload = saved


def _get(srv, path="/usage"):
    url = f"http://127.0.0.1:{srv.server_address[1]}{path}"
    try:
        with urllib.request.urlopen(url, timeout=5) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def test_loopback_gets_200(server):
    code, body = _get(server)
    assert code == 200
    assert json.loads(body) == {"s": 42}


def test_lan_ip_gets_empty_403(server, capsys):
    server.fake_addr = (LAN_IP, 5555)
    assert _get(server) == (403, b"")
    assert _get(server, "/anything-else") == (403, b"")  # checked before routing
    assert len(_refusals(capsys)) == 1


def test_allow_listed_lan_ip_gets_200(server):
    server.fake_addr = (LAN_IP, 5555)
    with patch.object(d, "HTTP_ALLOW_IPS", d.parse_allow_ips(LAN_IP)):
        code, body = _get(server)
    assert code == 200 and json.loads(body) == {"s": 42}


def test_token_still_checked_after_allow_list(server):
    with patch.object(d, "HTTP_TOKEN", "sekret"):
        assert _get(server)[0] == 403
        assert _get(server, "/usage?token=sekret")[0] == 200


# --- refusal log rate limit --------------------------------------------------

def test_first_refusal_logs_and_repeat_is_suppressed(server, capsys):
    server.fake_addr = (LAN_IP, 5555)
    _get(server)
    _get(server)
    lines = _refusals(capsys)
    assert len(lines) == 1 and lines[0].endswith(f"refused /usage from {LAN_IP}")


def test_refusal_logs_again_after_window(capsys):
    with patch.object(d.time, "monotonic", side_effect=[1000.0, 1030.0, 1061.0]):
        d._log_refusal(LAN_IP)
        d._log_refusal(LAN_IP)
        d._log_refusal(LAN_IP)
    assert len(_refusals(capsys)) == 2


def test_new_ip_logs_promptly_while_another_is_suppressed(capsys):
    d._log_refusal(LAN_IP)
    d._log_refusal(LAN_IP)
    d._log_refusal("192.168.1.77")
    assert [l.split()[-1] for l in _refusals(capsys)] == [LAN_IP, "192.168.1.77"]


def test_refusal_history_is_bounded():
    with patch.object(d, "REFUSAL_LOG_MAX_IPS", 3):
        for i in range(10):
            d._log_refusal(f"10.0.0.{i}")
    assert list(d._refusal_logged_at) == ["10.0.0.7", "10.0.0.8", "10.0.0.9"]
