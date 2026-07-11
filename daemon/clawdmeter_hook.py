#!/usr/bin/env python3
"""Clawdmeter working-indicator hook.

Writes a minimal per-session phase (running/idle) + last-active timestamp to
~/.clawdmeter/state.json so the daemon can light the splash "work coding" badge
and gate the usage-screen verb ticker. No other per-session data is collected
(the Activity/Approval screens were removed). Fail-open: always prints `{}` so
Claude Code proceeds unimpeded.

Registered in ~/.claude/settings.json for PreToolUse / UserPromptSubmit / Stop /
SessionStart.
"""

import fcntl
import json
import os
import sys
import time
from pathlib import Path

STATE_DIR = Path.home() / ".clawdmeter"
STATE_FILE = STATE_DIR / "state.json"
LOCK_FILE = STATE_DIR / "state.lock"

# Drop a session from the working-set once it's been quiet this long.
SESSION_TTL_SECONDS = 15 * 60


def _now() -> int:
    return int(time.time())


def _prune(sessions: dict, now: int) -> dict:
    return {
        sid: s for sid, s in sessions.items()
        if now - s.get("last_active_ts", 0) < SESSION_TTL_SECONDS
    }


def _update(payload: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    session_id = payload.get("session_id") or "unknown"
    event = payload.get("hook_event_name", "")
    now = _now()

    with open(LOCK_FILE, "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            state = json.loads(STATE_FILE.read_text())
            if not isinstance(state, dict) or "sessions" not in state:
                state = {"sessions": {}}
        except (OSError, json.JSONDecodeError):
            state = {"sessions": {}}

        sessions = _prune(state.get("sessions", {}), now)
        s = sessions.get(session_id, {"phase": "idle", "last_active_ts": 0})

        # Any tool call or new prompt => running; end of turn / fresh start => idle.
        if event in ("PreToolUse", "PostToolUse", "UserPromptSubmit"):
            s["phase"] = "running"
        elif event in ("Stop", "SessionStart"):
            s["phase"] = "idle"
        s["last_active_ts"] = now

        sessions[session_id] = s
        state["sessions"] = sessions
        tmp = STATE_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, separators=(",", ":")))
        os.replace(tmp, STATE_FILE)


def main() -> int:
    try:
        raw = sys.stdin.read()
        if raw.strip():
            payload = json.loads(raw)
            if isinstance(payload, dict):
                _update(payload)
    except Exception:
        # Fail-open — never block Claude Code on our errors.
        pass
    finally:
        sys.stdout.write("{}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
