#!/usr/bin/env python3
"""Clawdmeter attention hook.

Feeds the device's aggregate "attention" signals — is any Claude Code session
BLOCKED on the user (permission prompt / plan approval / AskUserQuestion),
FAILED (StopFailure), or finished-and-waiting (your turn) — plus the plain
running/idle phase for the splash working badge. Writes a compact per-session
record to ~/.clawdmeter/state.json; the daemon AGGREGATES across sessions into
a handful of counters (no per-session UI — that was removed on purpose).

DETECT-ONLY GUARANTEE: this hook NEVER returns a permission decision. main()'s
`finally` unconditionally writes `{}`, and no branch does I/O beyond the
state.json lock (no transcript scan, no network, no subprocess) — so even on
PermissionRequest (which runs BEFORE the approval dialog renders) it adds no
measurable latency to the real prompt.

Blocked tracking uses a pending SET keyed by (agent_id, tool_name) so a parallel
subagent's PostToolUse can't wrongly clear a sibling's prompt.
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

SESSION_TTL_SECONDS = 15 * 60
PENDING_TTL_SECONDS = 2 * 60 * 60   # a blocked session lingers this long (user may be away)

# Tools whose PreToolUse means "blocked on the user" (no PostToolUse until the
# user responds): a plan hand-off and a direct question.
BLOCKING_TOOLS = {"AskUserQuestion", "ExitPlanMode"}

NEW_DEFAULTS = {
    "pending": {},      # {"<agent_id>\x1f<tool>": {"ts": int}} — non-empty => blocked on you
    "failed_ts": 0,     # unix ts of last StopFailure (0 = none)
    "idle_ts": 0,       # unix ts this session finished a turn & is waiting for you (0 = none)
}


def _now() -> int:
    return int(time.time())


def _short_project(cwd):
    return Path(cwd).name if cwd else ""


def _prune(sessions: dict, now: int) -> dict:
    """Drop stale sessions; a blocked session is exempt from the normal 15-min
    TTL (kept up to 2h) so a pending decision doesn't vanish while you're away."""
    kept = {}
    for sid, s in sessions.items():
        age = now - s.get("last_active_ts", 0)
        ttl = PENDING_TTL_SECONDS if s.get("pending") else SESSION_TTL_SECONDS
        if age < ttl:
            kept[sid] = s
    return kept


def _default_session() -> dict:
    base = {"phase": "idle", "last_active_ts": 0, "project": ""}
    base.update({k: (dict(v) if isinstance(v, dict) else v) for k, v in NEW_DEFAULTS.items()})
    return base


def _update(payload: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    session_id = payload.get("session_id") or "unknown"
    event = payload.get("hook_event_name", "")
    tool_name = payload.get("tool_name", "")
    agent_id = payload.get("agent_id") or ""
    pkey = f"{agent_id}\x1f{tool_name}"
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

        if event == "SessionEnd":
            sessions.pop(session_id, None)
            state["sessions"] = sessions
            tmp = STATE_FILE.with_suffix(".tmp")
            tmp.write_text(json.dumps(state, separators=(",", ":")))
            os.replace(tmp, STATE_FILE)
            return

        s = sessions.get(session_id) or _default_session()
        for k, v in NEW_DEFAULTS.items():
            s.setdefault(k, dict(v) if isinstance(v, dict) else v)
        if "cwd" in payload:
            s["project"] = _short_project(payload["cwd"])
        s["last_active_ts"] = now

        if event == "PreToolUse" and tool_name in BLOCKING_TOOLS:
            s["pending"][pkey] = {"ts": now}
            s["phase"] = "running"
            s["failed_ts"] = 0
            s["idle_ts"] = 0
        elif event == "PreToolUse":
            s["phase"] = "running"
            s["failed_ts"] = 0
            s["idle_ts"] = 0
        elif event == "PermissionRequest":
            # A permission/plan dialog is about to render — record the block ONLY
            # (detect-only; never a decision). PreToolUse already set phase.
            s["pending"][pkey] = {"ts": now}
        elif event in ("PostToolUse", "PostToolUseFailure"):
            s["pending"].pop(pkey, None)   # this tool resolved (approved->ran, or ran->failed)
            s["phase"] = "running"
            s["idle_ts"] = 0
        elif event == "PermissionDenied":
            s["pending"].pop(pkey, None)
        elif event == "UserPromptSubmit":
            s["phase"] = "running"
            s["pending"] = {}
            s["failed_ts"] = 0
            s["idle_ts"] = 0
        elif event == "Stop":
            # Turn ended normally with nothing pending => "your turn" (waiting).
            s["phase"] = "idle"
            s["pending"] = {}
            s["failed_ts"] = 0
            s["idle_ts"] = now
        elif event == "StopFailure":
            s["phase"] = "idle"
            s["pending"] = {}
            s["idle_ts"] = 0
            s["failed_ts"] = now
        elif event == "SessionStart":
            s["phase"] = "idle"
            s["pending"] = {}
            s["failed_ts"] = 0
            s["idle_ts"] = 0

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
        pass
    finally:
        sys.stdout.write("{}\n")   # detect-only: never emit a decision
    return 0


if __name__ == "__main__":
    sys.exit(main())
