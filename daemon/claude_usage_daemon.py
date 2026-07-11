#!/usr/bin/env python3
"""Claude Usage Tracker Daemon (BLE) — macOS port of claude-usage-daemon.sh.

Polls Claude API rate-limit headers and writes a JSON payload to the
ESP32 "Clawdmeter" peripheral over a custom GATT service. Uses
bleak (CoreBluetooth backend on macOS).
"""

import asyncio
import getpass
import json
import os
import re
import signal
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx
from bleak import BleakClient, BleakScanner
from bleak.exc import BleakError

DEVICE_NAME = "Clawdmeter"
SERVICE_UUID = "4c41555a-4465-7669-6365-000000000001"
RX_CHAR_UUID = "4c41555a-4465-7669-6365-000000000002"
REQ_CHAR_UUID = "4c41555a-4465-7669-6365-000000000004"

POLL_INTERVAL = 60
TICK = 5
SCAN_TIMEOUT = 8.0

# macOS: token lives in Keychain (service "Claude Code-credentials").
# Linux: token lives in ~/.claude/.credentials.json.
KEYCHAIN_SERVICE = "Claude Code-credentials"
CREDENTIALS_PATH = Path.home() / ".claude" / ".credentials.json"
SAVED_ADDR_FILE = Path.home() / ".config" / "claude-usage-monitor" / "ble-address"
STATE_FILE = Path.home() / ".clawdmeter" / "state.json"  # written by clawdmeter_hook.py
WORKING_STALE_SECONDS = 120  # ignore a session's phase=="running" older than this (missed-Stop guard)
SESSION_LIST_SECONDS = 15 * 60  # keep idle sessions in the Activity list this long (matches the hook's TTL) so idle-time stays meaningful
MAX_SESSIONS = 5             # cap the Activity-screen list sent over BLE
MAX_BLE_PAYLOAD = 480        # keep under NimBLE's 512-byte attribute cap
# Auto-recovery: if no successful BLE write lands for this long, exit non-zero so
# launchd relaunches us with a fresh CoreBluetooth stack — the proven fix after
# the Mac sleeps and the link wedges. (plist KeepAlive restarts us ~10s later.)
STALE_RESTART_SECONDS = 300

# --- WiFi fallback transport -------------------------------------------------
# A tiny LAN HTTP endpoint serving the same payload the device gets over BLE, so
# the screen keeps updating when it's out of Bluetooth range (see the firmware's
# wifi_transport.cpp). Bound to all interfaces so the device can reach it on the
# local network. The daemon stamps its own LAN IP + this port into the BLE
# payload, so the device learns where to pull from with no hardcoded address.
HTTP_HOST = "0.0.0.0"
HTTP_PORT = int(os.environ.get("CLAWDMETER_PORT", "47800"))
# Optional shared secret. If set (here or via the env var), /usage requires a
# matching ?token=. Must equal WIFI_TOKEN in the firmware's wifi_cfg.h. Empty =
# open endpoint (fine on a trusted home/office LAN).
HTTP_TOKEN = os.environ.get("CLAWDMETER_TOKEN", "")

API_URL = "https://api.anthropic.com/v1/messages"
API_HEADERS_TEMPLATE = {
    "anthropic-version": "2023-06-01",
    "anthropic-beta": "oauth-2025-04-20",
    "Content-Type": "application/json",
    "User-Agent": "claude-code/2.1.5",
}
API_BODY = {
    "model": "claude-haiku-4-5-20251001",
    "max_tokens": 1,
    "messages": [{"role": "user", "content": "hi"}],
}


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def _extract_access_token(blob: str) -> str | None:
    """Pull the accessToken out of a credentials blob.

    Claude Code stores credentials as a JSON object; the blob may also be
    nested ({"claudeAiOauth": {"accessToken": "..."}}). Fall back to a
    regex match so unexpected shapes still work, and finally treat the
    blob as a raw token if nothing else matches.
    """
    blob = blob.strip()
    if not blob:
        return None
    try:
        data = json.loads(blob)
    except json.JSONDecodeError:
        data = None
    if isinstance(data, dict):
        # direct: {"accessToken": "..."}
        if isinstance(data.get("accessToken"), str):
            return data["accessToken"]
        # nested: {"claudeAiOauth": {"accessToken": "..."}}
        for v in data.values():
            if isinstance(v, dict) and isinstance(v.get("accessToken"), str):
                return v["accessToken"]
    m = re.search(r'"accessToken"\s*:\s*"([^"]+)"', blob)
    if m:
        return m.group(1)
    # Raw token (no JSON wrapper) — must look plausible (sk-ant-... etc.)
    if re.fullmatch(r"[A-Za-z0-9_\-.~+/=]{20,}", blob):
        return blob
    return None


def _read_token_keychain() -> str | None:
    try:
        out = subprocess.run(
            [
                "security",
                "find-generic-password",
                "-s",
                KEYCHAIN_SERVICE,
                "-a",
                getpass.getuser(),
                "-w",
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except subprocess.CalledProcessError as e:
        log(f"Keychain read failed (rc={e.returncode}): {e.stderr.strip()}")
        return None
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        log(f"Keychain access error: {e}")
        return None
    return _extract_access_token(out.stdout)


def _read_token_file() -> str | None:
    try:
        raw = CREDENTIALS_PATH.read_text()
    except OSError as e:
        log(f"Error reading credentials: {e}")
        return None
    return _extract_access_token(raw)


def read_token() -> str | None:
    if sys.platform == "darwin":
        return _read_token_keychain()
    return _read_token_file()


def state_mtime() -> float:
    try:
        return STATE_FILE.stat().st_mtime
    except OSError:
        return 0.0


def read_working() -> bool:
    """True if any Claude Code session is phase=='running' and recently active.

    Reads ~/.clawdmeter/state.json written by clawdmeter_hook.py. The staleness
    guard means a missed Stop hook (e.g. a crash) clears 'working' within
    WORKING_STALE_SECONDS rather than sticking on forever. A session blocked on
    an approval (`pending`) is excluded — it isn't actually running, and the
    device's verb-ticker shouldn't cycle while Claude waits on the user.
    """
    try:
        state = json.loads(STATE_FILE.read_text())
    except (OSError, json.JSONDecodeError):
        return False
    now = time.time()
    for s in state.get("sessions", {}).values():
        if (s.get("phase") == "running" and not s.get("pending")
                and (now - s.get("last_active_ts", 0)) < WORKING_STALE_SECONDS):
            return True
    return False


def _sanitize(text: str) -> str:
    """Keep only printable ASCII, collapse whitespace runs to a single space,
    strip. Session fields (prompts, tool args, todo text) are free-form user/
    model text and can carry newlines, control bytes, or non-ASCII — none of
    which the device's fixed-width display can render safely."""
    if not text:
        return ""
    cleaned = "".join(c if " " <= c <= "~" else " " for c in text)
    return " ".join(cleaned.split())


def _clean_prompt(p: str) -> str:
    """The user's last prompt, or "" if missing or if it looks like injected
    agent XML (e.g. '<task-notification>...') rather than something the user
    actually typed — live state.json shows last_user_prompt often isn't."""
    if not p or p.lstrip().startswith("<"):
        return ""
    return _sanitize(p)


def _latest_pending(s: dict) -> dict | None:
    """The most recently raised pending-approval entry for a session, or None."""
    pending = s.get("pending")
    if not isinstance(pending, dict) or not pending:
        return None
    return max(pending.values(), key=lambda p: p.get("ts", 0) if isinstance(p, dict) else 0)


def _activity_summary(s: dict, state: str) -> str:
    """One-line 'what this session is doing / just did', tailored to its
    derived device state — the row label on the Activity screen.

    needs_input: the most recent pending approval's ask. failed: the error
    type. working: the in-progress todo, else the current tool. completed:
    the last tool that ran, else the user's last prompt (if it isn't injected
    XML), else the session title, else "Idle"."""
    if state == "needs_input":
        ask = (_latest_pending(s) or {}).get("ask", "")
        text = f"Waiting: {ask}" if ask else "Waiting"
    elif state == "failed":
        err = (s.get("error_type") or "").strip()
        text = f"Failed: {err}" if err else "Failed"
    elif state == "working":
        _, _, todo_now = _todo_progress(s)
        if todo_now:
            text = todo_now
        else:
            tool = (s.get("current_tool") or "").strip()
            args = (s.get("current_tool_args") or "").strip()
            text = f"{tool} {args}".strip() if tool else "Working"
    else:  # completed
        last_tool = (s.get("last_tool_name") or "").strip()
        if last_tool:
            args = (s.get("last_tool_args") or "").strip()
            text = f"{last_tool} {args}".strip()
        else:
            text = (_clean_prompt(s.get("last_user_prompt") or "")
                     or (s.get("session_title") or "").strip()
                     or "Idle")
    return _sanitize(text)[:32]  # fits the firmware summary[36] field + 5-session BLE budget


def _todo_progress(s: dict):
    """Return (done, total, in_progress_text) for a session's to-do list."""
    todos = s.get("todos") or []
    if not isinstance(todos, list):
        return 0, 0, ""
    done = sum(1 for t in todos if isinstance(t, dict) and t.get("status") == "completed")
    now_txt = ""
    for t in todos:
        if isinstance(t, dict) and t.get("status") == "in_progress":
            now_txt = (t.get("activeForm") or t.get("content") or "")[:38]
            break
    return done, len(todos), now_txt


# 1-char device state codes (wire contract v2.0). Priority when several
# conditions hold at once: pending approval > running > failed > completed.
_STATE_CODE = {"needs_input": "n", "working": "w", "completed": "c", "failed": "f"}


def read_sessions() -> list:
    """Per-session activity list for the device's Activity screen.

    Reads ~/.clawdmeter/state.json, keeps sessions seen within
    SESSION_LIST_SECONDS (so idle sessions stay listed long enough for their
    idle-time to be meaningful), derives each session's 1-char state, sorts
    urgent sessions (needs_input/failed) first and most-recently-active
    otherwise, caps at MAX_SESSIONS, and emits short keys to keep the BLE
    payload small.

    LEAN tier (always sent, both transports): id=first 4 chars of the session
    id, ss=1-char state code, i=idle seconds. RICH/detail tier (HTTP always;
    BLE only if it fits, stripped oldest-first by fit_sessions): a=state-aware
    activity summary, ak=approval ask (needs_input sessions only), e=effort,
    c=context-%, td/tt=todo done/total, tn=in-progress todo, p=project.
    """
    try:
        state = json.loads(STATE_FILE.read_text())
    except (OSError, json.JSONDecodeError):
        return []
    sessions = state.get("sessions", {})
    if not isinstance(sessions, dict):
        return []
    now = time.time()
    fresh = [(sid, s) for sid, s in sessions.items()
             if isinstance(s, dict) and (now - s.get("last_active_ts", 0)) < SESSION_LIST_SECONDS]

    rows = []
    for sid, s in fresh:
        last_active_ts = s.get("last_active_ts", 0)
        idle = int(max(0, now - last_active_ts))
        # A session whose phase is "running" but which hasn't moved in
        # WORKING_STALE_SECONDS (missed Stop) reads as idle, not stuck-running.
        running = s.get("phase") == "running" and idle < WORKING_STALE_SECONDS
        if s.get("pending"):
            state_name = "needs_input"
        elif running:
            state_name = "working"
        elif s.get("outcome") == "failed":
            state_name = "failed"
        else:
            state_name = "completed"
        rows.append((sid, s, state_name, idle, last_active_ts))

    # Urgent first so fit_sessions, which trims from the tail, never sacrifices
    # an urgent session to the MAX_SESSIONS cap.
    rows.sort(key=lambda r: (0 if r[2] in ("needs_input", "failed") else 1, -r[4]))

    out = []
    for sid, s, state_name, idle, _last_active_ts in rows[:MAX_SESSIONS]:
        done, total, todo_now = _todo_progress(s)
        entry = {
            "id": sid[:4] if sid else "?",
            "ss": _STATE_CODE[state_name],
            "i": idle,
            "a": _activity_summary(s, state_name),
            "e": (s.get("effort") or "")[:7],
            "c": int(s.get("ctx_pct", 0) or 0),
            "td": done,
            "tt": total,
            "tn": todo_now,
            "p": (s.get("project") or "")[:23],
        }
        if state_name == "needs_input":
            entry["ak"] = _sanitize((_latest_pending(s) or {}).get("ask", ""))[:46]
        out.append(entry)
    return out


# Keys that make up the lightweight, always-sent part of a session entry.
# `a` (the short activity summary / row label) rides in the always-sent lean
# tier so every Activity row is labelled even over BLE — the device's WiFi feed
# is a stale-BLE fallback (20s takeover), so BLE stays the primary render path.
_SESSION_LIST_KEYS = ("id", "ss", "i", "a")
# Detail keys, dropped oldest-first when the payload would exceed MAX_BLE_PAYLOAD.
_SESSION_DETAIL_KEYS = ("ak", "e", "c", "td", "tt", "tn", "p")


def fit_sessions(base_payload: dict, sessions: list) -> list:
    """Trim a session list so the full serialized payload stays under
    MAX_BLE_PAYLOAD. The lightweight list (p/m/c/w/i for every session) is
    always kept; detail fields (a/td/tt/tn) are dropped from the
    oldest sessions first until it fits. Returns the (possibly trimmed) list."""
    sessions = [dict(s) for s in sessions]  # don't mutate caller's dicts

    def size_with(sess):
        cand = dict(base_payload)
        cand["sessions"] = sess
        return len(json.dumps(cand, separators=(",", ":")))

    # Strip detail from the end (oldest) until it fits, or all detail is gone.
    i = len(sessions) - 1
    while size_with(sessions) > MAX_BLE_PAYLOAD and i >= 0:
        if any(k in sessions[i] for k in _SESSION_DETAIL_KEYS):
            for k in _SESSION_DETAIL_KEYS:
                sessions[i].pop(k, None)
        else:
            i -= 1  # this one is already list-only; move to the next-oldest
    # If even the bare list overflows, drop whole sessions (oldest first).
    while sessions and size_with(sessions) > MAX_BLE_PAYLOAD:
        sessions = sessions[:-1]
    return sessions


def load_cached_address() -> str | None:
    if not SAVED_ADDR_FILE.exists():
        return None
    addr = SAVED_ADDR_FILE.read_text().strip()
    # Accept both Linux MAC (AA:BB:CC:DD:EE:FF) and macOS CoreBluetooth UUID
    # (E621E1F8-C36C-495A-93FC-0C247A3E6E5F).
    if re.fullmatch(r"(?:[0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}", addr) or re.fullmatch(
        r"[0-9A-Fa-f]{8}-(?:[0-9A-Fa-f]{4}-){3}[0-9A-Fa-f]{12}", addr
    ):
        return addr
    log("Cached address malformed, discarding")
    SAVED_ADDR_FILE.unlink(missing_ok=True)
    return None


def save_address(addr: str) -> None:
    SAVED_ADDR_FILE.parent.mkdir(parents=True, exist_ok=True)
    SAVED_ADDR_FILE.write_text(addr)


async def scan_for_device() -> str | None:
    log(f"Scanning for '{DEVICE_NAME}' ({SCAN_TIMEOUT}s)...")
    devices = await BleakScanner.discover(timeout=SCAN_TIMEOUT)
    for d in devices:
        if d.name == DEVICE_NAME:
            log(f"Found: {d.address}")
            return d.address
    return None


# --- macOS: recover a device the OS already holds as an HID keyboard --------
#
# The firmware advertises as a BLE HID keyboard so its buttons type into the
# Mac. macOS auto-connects to that HID, and CoreBluetooth then EXCLUDES the
# peripheral from BleakScanner.discover() results (already-connected devices
# never appear in scans). bleak's connect-by-address path also scans
# internally, so a cached address can't help either. The documented escape
# hatch is retrieveConnectedPeripheralsWithServices_, which returns
# peripherals the system is already connected to. We wrap the result in a
# BLEDevice carrying the live (peripheral, manager) details so BleakClient
# connects to it directly without scanning. CoreBluetooth shares the single
# physical link, so this rides the existing HID connection — the keyboard
# keeps working.
_cb_manager = None  # reused CentralManagerDelegate (CoreBluetooth)
_last_success_ts = 0.0  # time.time() of the last successful BLE write (for the watchdog)

# Shared latest payload(s) — produced by the poller task, consumed by BOTH the
# BLE sender and the HTTP server. Guarded by a plain threading.Lock because the
# HTTP handler runs in its own thread; the asyncio side only holds it briefly
# (never across an await). The two asyncio.Events are created in main() once a
# loop is running.
#
# Two tiers: _latest_payload is the lean, MAX_BLE_PAYLOAD-capped session list
# (what BLE sends); _latest_rich_payload carries the FULL, uncapped session
# list for the WiFi/HTTP consumer, which isn't bound by the BLE attribute cap
# and prefers the richer feed when it's on the local network.
_latest_payload: dict | None = None
_latest_rich_payload: dict | None = None
_latest_lock = threading.Lock()
_payload_updated: asyncio.Event | None = None  # poller → BLE sender: a push-worthy change landed
_force_poll: asyncio.Event | None = None        # BLE sender → poller: device asked for a refresh


def reset_cb_manager() -> None:
    """Drop the cached CoreBluetooth central so the next lookup builds a fresh
    one — clears a manager that went stale across a Mac sleep/wake."""
    global _cb_manager
    _cb_manager = None


async def _get_cb_manager():
    """Lazily create and ready a shared CoreBluetooth central manager."""
    global _cb_manager
    if _cb_manager is None:
        from bleak.backends.corebluetooth.CentralManagerDelegate import (
            CentralManagerDelegate,
        )

        mgr = CentralManagerDelegate()
        await mgr.wait_until_ready()  # raises if Bluetooth is unauthorized/off
        _cb_manager = mgr
    return _cb_manager


async def retrieve_connected_macos(skip_addr: str | None = None):
    """Return a BLEDevice for a system-connected 'Claude Controller', or None.

    Two-step lookup, strongest signal first:

    1. Peripherals connected under our CUSTOM service UUID. Membership in
       that service is unambiguous (no other device exposes it), so we accept
       by service alone — the peripheral's name can be None on macOS.
    2. Fall back to the generic HID service 0x1812, but ONLY trust a
       peripheral whose name matches DEVICE_NAME. 0x1812 also matches
       unrelated keyboards/mice, so picking blindly here could grab the
       wrong device.

    ``skip_addr`` skips a peripheral whose UUID just failed to connect, so a
    stale CoreBluetooth handle can't trap us into never trying a fresh scan.
    """
    from CoreBluetooth import CBUUID
    from bleak.backends.device import BLEDevice

    try:
        manager = await _get_cb_manager()
    except Exception as e:  # BleakBluetoothNotAvailableError etc.
        log(f"CoreBluetooth unavailable: {e}")
        return None

    cm = manager.central_manager

    def _wrap(p):
        addr = p.identifier().UUIDString()
        log(f"Found system-connected peripheral: {p.name()!r} [{addr}]")
        return BLEDevice(addr, p.name(), (p, manager))

    def _ok(p) -> bool:
        return not (skip_addr and p.identifier().UUIDString() == skip_addr)

    # 1. Custom service — accept by service membership alone.
    custom = cm.retrieveConnectedPeripheralsWithServices_(
        [CBUUID.UUIDWithString_(SERVICE_UUID)]
    )
    for p in custom or []:
        if _ok(p):
            return _wrap(p)

    # 2. Generic HID service — require an exact name match.
    hid = cm.retrieveConnectedPeripheralsWithServices_(
        [CBUUID.UUIDWithString_("1812")]
    )
    for p in hid or []:
        if _ok(p) and p.name() == DEVICE_NAME:
            return _wrap(p)

    return None


async def discover_target(skip_addr: str | None = None):
    """Return a connectable target, or None.

    macOS: prefer the system-connected peripheral (HID-grabbed devices are
    invisible to scans); fall back to a normal scan that yields a BLEDevice
    so the subsequent connect doesn't have to re-scan. ``skip_addr`` is
    forwarded so a just-failed peripheral is skipped, making the scan
    fallback reachable.

    Other platforms: keep the original cached-address / scan-by-name flow.
    A freshly scanned address is cached here (the only place it's saved).
    """
    if sys.platform == "darwin":
        dev = await retrieve_connected_macos(skip_addr=skip_addr)
        if dev is not None:
            return dev
        log(f"Not held by OS; scanning for '{DEVICE_NAME}' ({SCAN_TIMEOUT}s)...")
        dev = await BleakScanner.find_device_by_name(DEVICE_NAME, timeout=SCAN_TIMEOUT)
        if dev:
            log(f"Found: {dev.address}")
        return dev

    address = load_cached_address()
    if not address:
        address = await scan_for_device()
        if address:
            save_address(address)  # cache only freshly-scanned addresses
    return address


async def poll_api(token: str) -> dict | None:
    headers = dict(API_HEADERS_TEMPLATE)
    headers["Authorization"] = f"Bearer {token}"
    try:
        async with httpx.AsyncClient(timeout=20.0) as http:
            resp = await http.post(API_URL, headers=headers, json=API_BODY)
    except httpx.HTTPError as e:
        log(f"API call failed: {e}")
        return None
    if resp.status_code >= 400:
        log(f"API HTTP {resp.status_code}: {resp.text[:200]}")
        return None

    def hdr(name: str, default: str = "0") -> str:
        return resp.headers.get(name, default)

    now = time.time()

    def reset_minutes(reset_ts: str) -> int:
        try:
            r = float(reset_ts)
        except ValueError:
            return 0
        mins = (r - now) / 60.0
        return int(round(mins)) if mins > 0 else 0

    def pct(util: str) -> int:
        try:
            return int(round(float(util) * 100))
        except ValueError:
            return 0

    payload = {
        "s": pct(hdr("anthropic-ratelimit-unified-5h-utilization")),
        "sr": reset_minutes(hdr("anthropic-ratelimit-unified-5h-reset")),
        "w": pct(hdr("anthropic-ratelimit-unified-7d-utilization")),
        "wr": reset_minutes(hdr("anthropic-ratelimit-unified-7d-reset")),
        "st": hdr("anthropic-ratelimit-unified-5h-status", "unknown"),
        "ok": True,
    }
    return payload


def lan_ip() -> str:
    """Best-effort primary LAN IPv4 of this host. Opens a UDP socket toward a
    public address (no packets are actually sent) so the OS picks the egress
    interface, then reads back the local address it chose."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


class _UsageHandler(BaseHTTPRequestHandler):
    """Serves the latest payload at GET /usage (token-checked if configured)."""

    def _send(self, code: int, body: bytes = b"") -> None:
        self.send_response(code)
        if body:
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if body:
            self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802 (BaseHTTPRequestHandler API)
        u = urlparse(self.path)
        if u.path != "/usage":
            self._send(404)
            return
        if HTTP_TOKEN and parse_qs(u.query).get("token", [""])[0] != HTTP_TOKEN:
            self._send(403)
            return
        with _latest_lock:
            # Prefer the uncapped rich feed (WiFi isn't byte-limited like BLE);
            # fall back to the lean payload if a rich one hasn't landed yet.
            payload = _latest_rich_payload or _latest_payload
        if payload is None:
            self._send(503)  # nothing polled yet
            return
        self._send(200, json.dumps(payload, separators=(",", ":")).encode())

    def log_message(self, *_args) -> None:  # silence per-request stderr spam
        pass


def start_http_server() -> None:
    try:
        srv = ThreadingHTTPServer((HTTP_HOST, HTTP_PORT), _UsageHandler)
    except OSError as e:
        log(f"HTTP server failed to bind {HTTP_HOST}:{HTTP_PORT}: {e} (WiFi fallback disabled)")
        return
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    extra = " [token required]" if HTTP_TOKEN else ""
    log(f"HTTP server on {lan_ip()}:{HTTP_PORT} (GET /usage){extra}")


class Session:
    def __init__(self, client: BleakClient) -> None:
        self.client = client
        self.refresh_requested = asyncio.Event()

    def _on_refresh(self, _char, _data: bytearray) -> None:
        log("Refresh requested by device")
        self.refresh_requested.set()

    async def setup_refresh_subscription(self) -> None:
        try:
            await self.client.start_notify(REQ_CHAR_UUID, self._on_refresh)
        except (BleakError, ValueError) as e:
            log(f"Refresh subscription unavailable: {e}")

    async def write_payload(self, payload: dict) -> bool:
        data = json.dumps(payload, separators=(",", ":")).encode()
        log(f"Sending: {data.decode()}")
        try:
            await self.client.write_gatt_char(RX_CHAR_UUID, data, response=True)
            return True
        except BleakError as e:
            log(f"Write failed: {e}")
            return False


async def _wait_any(events, timeout: float) -> None:
    """Return when any of the given asyncio.Events is set, or after `timeout`
    seconds. `None` entries are ignored (events may not exist yet at startup)."""
    events = [e for e in events if e is not None]
    if not events:
        await asyncio.sleep(timeout)
        return
    tasks = [asyncio.create_task(e.wait()) for e in events]
    try:
        await asyncio.wait(tasks, timeout=timeout, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for t in tasks:
            t.cancel()


async def poller(stop_event: asyncio.Event) -> None:
    """Poll the API + read the hook's state.json on a cadence, assemble the
    lean (BLE-capped) and rich (uncapped) payloads, and publish them to
    _latest_payload / _latest_rich_payload respectively.

    Runs independently of any BLE connection — that's what makes the WiFi
    endpoint work when the device is out of Bluetooth range. Both payloads are
    refreshed every cycle (so HTTP stays current), but the BLE sender is only
    nudged (via _payload_updated) on a push-worthy change, preserving the
    original poll-or-state-change push policy and BLE write frequency.
    """
    global _latest_payload, _latest_rich_payload
    last_poll = 0.0
    api_payload: dict | None = None
    last_state_mtime = state_mtime()
    pushed_working: bool | None = None
    pushed_sessions: list | None = None
    while not stop_event.is_set():
        now = time.time()
        forced = _force_poll is not None and _force_poll.is_set()
        if forced:
            _force_poll.clear()
        do_poll = forced or (now - last_poll) >= POLL_INTERVAL
        if do_poll:
            token = read_token()
            if not token:
                log("No token; skipping poll")
            else:
                p = await poll_api(token)
                if p is not None:
                    api_payload = p
                    last_poll = time.time()

        if api_payload is not None:
            mt = state_mtime()
            state_changed = mt != last_state_mtime
            last_state_mtime = mt
            working = read_working()
            sessions = read_sessions()
            # Stamp our LAN address so the device learns where to pull over WiFi.
            base = {k: v for k, v in api_payload.items() if k != "sessions"}
            base["working"] = working
            base["host"] = lan_ip()
            base["port"] = HTTP_PORT
            # Rich tier: the full session list, uncapped, for HTTP/WiFi.
            rich_payload = dict(base)
            rich_payload["sessions"] = sessions
            # Lean tier: keep the payload under the BLE attribute cap
            # (host/port included in the budget).
            lean_sessions = fit_sessions(base, sessions)
            lean_payload = dict(base)
            lean_payload["sessions"] = lean_sessions
            with _latest_lock:
                _latest_payload = lean_payload
                _latest_rich_payload = rich_payload
            # Push-worthy change is judged on the lean (BLE) session list —
            # unchanged BLE write frequency/policy.
            push = do_poll or (state_changed and (
                working != pushed_working or lean_sessions != pushed_sessions))
            if push:
                pushed_working = working
                pushed_sessions = lean_sessions
                if _payload_updated is not None:
                    _payload_updated.set()

        await _wait_any([stop_event, _force_poll], TICK)


async def connect_and_run(target, stop_event: asyncio.Event) -> bool:
    """Connect to the device and stream the shared payload over BLE until
    disconnected or stopped. Polling now lives in poller(); this function only
    SENDS, so it stays responsive and the API is polled regardless of BLE.

    ``target`` is either an address string (Linux) or a BLEDevice carrying live
    CoreBluetooth details (macOS). Returns True if at least one write succeeded
    (so the caller keeps the cached address), False if the connection failed.
    """
    global _last_success_ts
    display = target if isinstance(target, str) else target.address
    log(f"Connecting to {display}...")
    client = BleakClient(target)
    try:
        await client.connect()
    except (BleakError, asyncio.TimeoutError) as e:
        log(f"Connection failed: {e}")
        return False

    if not client.is_connected:
        log("Connection failed (no error but not connected)")
        return False

    log("Connected")
    session = Session(client)
    await session.setup_refresh_subscription()

    last_sent: dict | None = None
    used_successfully = False
    try:
        while client.is_connected and not stop_event.is_set():
            if _payload_updated is not None:
                _payload_updated.clear()  # clear before reading → no lost wakeups
            # A device refresh request forces the poller to poll immediately.
            if session.refresh_requested.is_set():
                session.refresh_requested.clear()
                if _force_poll is not None:
                    _force_poll.set()

            with _latest_lock:
                payload = dict(_latest_payload) if _latest_payload is not None else None
            if payload is not None and payload != last_sent:
                if await session.write_payload(payload):
                    used_successfully = True
                    _last_success_ts = time.time()
                    last_sent = payload

            # Wake on the next poller update, a device refresh, or stop; cap the
            # wait so a missed signal can't wedge us.
            await _wait_any(
                [stop_event, session.refresh_requested, _payload_updated], TICK)
    finally:
        try:
            await client.disconnect()
        except BleakError:
            pass

    log("Device disconnected" if not stop_event.is_set() else "Stopping")
    return used_successfully


async def watchdog(stop_event: asyncio.Event) -> None:
    """Self-heal: if the BLE link wedges (no successful write for a while — e.g.
    after the Mac sleeps and CoreBluetooth gets stuck), exit non-zero so launchd
    relaunches us with a fresh stack. Automates the manual `launchctl kickstart`."""
    while not stop_event.is_set():
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=30)
        except asyncio.TimeoutError:
            pass
        if stop_event.is_set():
            return
        stale = time.time() - _last_success_ts
        if stale > STALE_RESTART_SECONDS:
            log(f"Watchdog: no successful update in {int(stale)}s — re-exec for a clean reconnect")
            sys.stdout.flush()
            sys.stderr.flush()
            # Replace this process with a fresh one (new CoreBluetooth stack).
            # Doesn't depend on launchd KeepAlive, so it works regardless.
            os.execv(sys.executable, [sys.executable] + sys.argv)


async def main() -> None:
    global _last_success_ts, _payload_updated, _force_poll
    _last_success_ts = time.time()  # grace period before the watchdog can fire
    _payload_updated = asyncio.Event()
    _force_poll = asyncio.Event()
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()

    def _stop(*_args: object) -> None:
        log("Daemon stopping")
        stop_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _stop)
        except NotImplementedError:
            signal.signal(sig, _stop)

    log("=== Claude Usage Tracker Daemon (BLE + WiFi, macOS) ===")
    log(f"Poll interval: {POLL_INTERVAL}s")

    start_http_server()
    loop.create_task(watchdog(stop_event))
    loop.create_task(poller(stop_event))

    backoff = 1
    skip_addr: str | None = None  # macOS: a peripheral to skip for one cycle
    async def backoff_wait() -> None:
        nonlocal backoff
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=backoff)
        except asyncio.TimeoutError:
            pass
        backoff = min(backoff * 2, 60)

    while not stop_event.is_set():
        try:
            # Apply any pending skip exactly once, then clear it so the next
            # cycle re-tries retrieveConnected (the device may have recovered).
            target = await discover_target(skip_addr=skip_addr)
            skip_addr = None
            if not target:
                log(f"Device not found, retrying in {backoff}s...")
                if sys.platform == "darwin" and backoff >= 16:
                    reset_cb_manager()  # refresh a possibly-stale central before the watchdog kicks in
                await backoff_wait()
                continue

            addr = target if isinstance(target, str) else target.address
            ok = await connect_and_run(target, stop_event)
            if not ok:
                if sys.platform == "darwin":
                    # No string cache to drop; instead skip this stale handle on
                    # the next retrieveConnected so the scan fallback is reachable.
                    skip_addr = addr
                    reset_cb_manager()  # rebuild a fresh central in case it went stale
                else:
                    log("Invalidating cached address")
                    SAVED_ADDR_FILE.unlink(missing_ok=True)
                await backoff_wait()
            else:
                backoff = 1
        except BleakError as e:
            # Bluetooth adapter unavailable (e.g. powered off / asleep) or a
            # transient stack fault. Previously this propagated out of
            # asyncio.run() and killed the daemon, leaving the screen dark
            # until a manual restart. Instead, wait and retry so the daemon
            # self-heals the moment Bluetooth comes back.
            log(f"Bluetooth unavailable ({e}); retrying in {backoff}s...")
            if sys.platform == "darwin":
                reset_cb_manager()  # drop the stale central; rebuild on next cycle
            await backoff_wait()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(0)
