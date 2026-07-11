#!/usr/bin/env python3
"""Claude Code hook entry point for Clawdmeter.

Registered in ~/.claude/settings.json as a `hooks` entry. On every hook
event Claude Code invokes this script and pipes a JSON payload to stdin.
We extract the bits the daemon cares about (state, todos, current tool,
model, cwd) into a per-session record in ~/.clawdmeter/state.json and exit
silently (fail-open: a stdout JSON `{}` lets Claude proceed unimpeded).

DETECT-ONLY GUARANTEE (Clawdmeter 2.0 Phase A): this hook NEVER returns a
permission decision. `main()`'s `finally` unconditionally writes `{}` — so
even when Claude Code invokes us on `PermissionRequest` (which runs BEFORE
the approval dialog renders), we only record that an approval is pending and
get out of the way. We also skip the transcript scan (`_compute_ctx_pct`) on
the permission-critical / high-frequency events and read only the transcript
TAIL, so we add no measurable latency to the real approval prompt.

STATE MODEL: each session carries a `pending` set of approvals it's blocked
on, keyed by (agent_id, tool_name) so a parallel subagent's PostToolUse can't
wrongly clear a sibling's prompt. The daemon derives the 1-char device state
from `pending` (needs_input) / `phase` (working) / `outcome` (completed|failed).
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

# Cap session retention so a forgotten session doesn't permanently occupy a
# slot in the device UI. A session BLOCKED on an approval is kept far longer
# (a real decision can take a while when the user is away) but still capped so
# a hard-killed terminal mid-prompt eventually ages out.
SESSION_TTL_SECONDS = 15 * 60
PENDING_TTL_SECONDS = 2 * 60 * 60

# Transcript tail window for the ctx-% scan — bounds the read regardless of how
# large the transcript grows (the latest usage block lives near the end).
CTX_TAIL_BYTES = 256 * 1024

# Events on which we recompute ctx-%. Deliberately EXCLUDES the permission
# events (PermissionRequest/PermissionDenied — must stay latency-free) and the
# high-frequency PostToolUse* (PreToolUse already keeps ctx fresh).
CTX_EVENTS = {"PreToolUse", "UserPromptSubmit", "Stop", "StopFailure", "SessionStart"}

# Tools whose PreToolUse means "blocked on the user" (no PostToolUse until the
# user responds): a plan hand-off and a direct question.
BLOCKING_TOOLS = {"AskUserQuestion", "ExitPlanMode"}

# Fields added in 2.0; back-fill onto pre-existing records via setdefault.
NEW_DEFAULTS = {
    "pending": {},          # {"<agent_id>\x1f<tool>": {"tool","ask","ts"}} — non-empty ⇒ needs_input
    "outcome": "",          # "" | "completed" | "failed" (used when not running & not pending)
    "last_tool_name": "",   # archived on turn-clear, for the daemon's completed-state summary
    "last_tool_args": "",
    "session_title": "",    # cached from SessionStart if the payload carries one (unverified)
    "error_type": "",       # StopFailure error_type, for the failed-state summary
}

# Context-window sizes by model. The transcript logs the model WITHOUT the
# 1M-variant marker, so this is best-effort; the safety rule in _compute_ctx_pct
# bumps to 1M when observed tokens already exceed the mapped window.
MODEL_WINDOW = {
    "claude-opus-4-8": 1_000_000, "claude-opus-4-7": 1_000_000, "claude-opus-4-6": 1_000_000,
    "claude-sonnet-4-6": 1_000_000,
    "claude-sonnet-4-5": 200_000, "claude-sonnet-4": 200_000,
    "claude-haiku-4-5": 200_000,
}
DEFAULT_WINDOW = 200_000


def _now() -> int:
    return int(time.time())


def _short_model(model: str | None) -> str:
    """Strip the date suffix so 'claude-sonnet-4-6-20250930' → 'sonnet-4-6'."""
    if not model:
        return ""
    name = model.replace("claude-", "")
    parts = name.split("-")
    # Drop trailing date-shaped chunk (8 digits)
    if parts and parts[-1].isdigit() and len(parts[-1]) >= 8:
        parts = parts[:-1]
    return "-".join(parts)


def _short_project(cwd: str | None) -> str:
    if not cwd:
        return ""
    return Path(cwd).name


def _ctx_window_for(raw_model: str) -> int:
    """Map a raw transcript model id (e.g. 'claude-opus-4-8-20260101') to its
    context-window size. Strips a trailing date chunk; keeps the 'claude-'
    prefix (MODEL_WINDOW keys include it)."""
    if not raw_model:
        return DEFAULT_WINDOW
    parts = raw_model.split("-")
    if parts and parts[-1].isdigit() and len(parts[-1]) >= 8:
        parts = parts[:-1]
    return MODEL_WINDOW.get("-".join(parts), DEFAULT_WINDOW)


def _compute_ctx_pct(transcript_path):
    """Approximate context-window % from the latest assistant turn's usage in
    the session transcript. Returns an int 0..100, or None on any problem
    (fail-open). Reads only the last CTX_TAIL_BYTES so the scan stays cheap on
    million-token transcripts. Called OUTSIDE the state.json lock."""
    if not transcript_path:
        return None
    try:
        size = os.path.getsize(transcript_path)
        with open(transcript_path, "rb") as fh:
            if size > CTX_TAIL_BYTES:
                fh.seek(size - CTX_TAIL_BYTES)
                fh.readline()  # discard the partial first line
            data = fh.read()
        last_usage = None
        last_model = ""
        for raw in data.splitlines():
            try:
                o = json.loads(raw)
            except (json.JSONDecodeError, ValueError):
                continue
            msg = o.get("message") if isinstance(o.get("message"), dict) else None
            if not msg:
                continue
            u = msg.get("usage")
            if isinstance(u, dict):
                last_usage = u
                last_model = msg.get("model") or last_model
        if not last_usage:
            return None
        tokens = (last_usage.get("input_tokens", 0)
                  + last_usage.get("cache_read_input_tokens", 0)
                  + last_usage.get("cache_creation_input_tokens", 0))
        window = _ctx_window_for(last_model)
        if tokens > window:
            window = 1_000_000
        pct = round(100 * tokens / window) if window else 0
        return max(0, min(100, pct))
    except OSError:
        return None


def _tool_args_summary(tool_name: str, tool_input: dict) -> str:
    """Return a short human-readable summary of a tool's most relevant
    arg (the thing you'd want to see next to the tool name on a 1-line
    display). Returns "" when the tool has nothing useful to show.
    """
    if not isinstance(tool_input, dict):
        return ""
    if tool_name == "Bash":
        return str(tool_input.get("command", ""))[:80]
    if tool_name in ("Read", "Write", "Edit"):
        path = str(tool_input.get("file_path", ""))
        return Path(path).name if path else ""
    if tool_name == "NotebookEdit":
        path = str(tool_input.get("notebook_path", ""))
        return Path(path).name if path else ""
    if tool_name in ("Grep", "Glob"):
        return str(tool_input.get("pattern", ""))[:60]
    if tool_name == "Task":
        return str(tool_input.get("description", ""))[:80]
    if tool_name == "WebFetch":
        return str(tool_input.get("url", ""))[:80]
    if tool_name == "WebSearch":
        return str(tool_input.get("query", ""))[:80]
    if tool_name == "SlashCommand":
        return str(tool_input.get("command", ""))[:80]
    return ""


def _display_tool(tool_name: str) -> str:
    if tool_name == "ExitPlanMode":
        return "Plan"
    if tool_name == "AskUserQuestion":
        return "Question"
    return tool_name or "approval"


def _ask_text(tool_name: str, tool_input: dict) -> str:
    """A short 'what is this blocked on' string for the Approval screen."""
    if tool_name == "ExitPlanMode":
        return "Plan"
    if tool_name == "AskUserQuestion":
        try:
            qs = tool_input.get("questions") or []
            hdr = qs[0].get("header") if qs and isinstance(qs[0], dict) else ""
        except (AttributeError, IndexError, TypeError):
            hdr = ""
        return ("Ask: " + str(hdr))[:60] if hdr else "Question"
    args = _tool_args_summary(tool_name, tool_input)
    return (f"{tool_name}: {args}" if args else (tool_name or "approval"))[:60]


def _archive_current(session: dict) -> None:
    """Snapshot the in-flight tool before a turn boundary clears it, so the
    daemon can label a just-finished session with what it was last doing."""
    if session.get("current_tool"):
        session["last_tool_name"] = session["current_tool"]
        session["last_tool_args"] = session.get("current_tool_args", "")


def _prune(sessions: dict, now: int) -> dict:
    """Drop stale sessions. A session blocked on an approval is exempt from the
    normal 15-min TTL (kept up to PENDING_TTL_SECONDS) so a pending decision
    doesn't silently vanish from the device while the user is away."""
    kept = {}
    for sid, s in sessions.items():
        age = now - s.get("last_active_ts", 0)
        ttl = PENDING_TTL_SECONDS if s.get("pending") else SESSION_TTL_SECONDS
        if age < ttl:
            kept[sid] = s
    return kept


def _default_session() -> dict:
    base = {
        "cwd": "",
        "project": "",
        "model": "",
        "effort": "",
        "last_tool": "",
        "current_tool": "",
        "current_tool_args": "",
        "phase": "idle",
        "last_user_prompt": "",
        "last_active_ts": 0,
        "ctx_pct": 0,
        "todos": [],
    }
    base.update({k: (dict(v) if isinstance(v, dict) else v) for k, v in NEW_DEFAULTS.items()})
    return base


def _update(payload: dict) -> None:
    """Apply a single hook payload to state.json."""
    STATE_DIR.mkdir(parents=True, exist_ok=True)

    session_id = payload.get("session_id") or "unknown"
    event = payload.get("hook_event_name", "")
    tool_name = payload.get("tool_name", "")
    tool_input = payload.get("tool_input") or {}
    agent_id = payload.get("agent_id") or ""
    pkey = f"{agent_id}\x1f{tool_name}"
    now = _now()

    # Compute ctx-% OUTSIDE the lock (only reads the transcript tail) and only
    # on events where it's worth refreshing — keeps the permission path fast.
    pct = _compute_ctx_pct(payload.get("transcript_path")) if event in CTX_EVENTS else None

    with open(LOCK_FILE, "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            state = json.loads(STATE_FILE.read_text())
            if not isinstance(state, dict) or "sessions" not in state:
                state = {"sessions": {}}
        except (OSError, json.JSONDecodeError):
            state = {"sessions": {}}

        sessions = state.get("sessions", {})
        sessions = _prune(sessions, now)

        if event == "SessionEnd":
            # Clean exit — drop the record immediately (no stale ghost).
            sessions.pop(session_id, None)
            state["sessions"] = sessions
            tmp = STATE_FILE.with_suffix(".tmp")
            tmp.write_text(json.dumps(state, separators=(",", ":")))
            os.replace(tmp, STATE_FILE)
            return

        session = sessions.get(session_id) or _default_session()
        for k, v in NEW_DEFAULTS.items():  # back-fill onto older records
            session.setdefault(k, dict(v) if isinstance(v, dict) else v)

        # Always refresh metadata that hook payloads carry.
        if "cwd" in payload:
            session["cwd"] = payload["cwd"]
            session["project"] = _short_project(payload["cwd"])
        if "model" in payload:
            session["model"] = _short_model(payload["model"])
        # Effort level (low/medium/high/xhigh/max) — present on tool-context
        # events; keep the prior value when absent.
        eff = payload.get("effort")
        if isinstance(eff, dict) and eff.get("level"):
            session["effort"] = str(eff["level"])[:7]
        session["last_active_ts"] = now
        if pct is not None:
            session["ctx_pct"] = pct

        if event == "PreToolUse" and tool_name == "TodoWrite":
            raw_todos = tool_input.get("todos") or []
            session["todos"] = [
                {
                    "content": str(t.get("content", ""))[:120],
                    "status": str(t.get("status", "pending")),
                    "activeForm": str(t.get("activeForm", ""))[:80],
                }
                for t in raw_todos if isinstance(t, dict)
            ]
            session["last_tool"] = "TodoWrite"
            session["current_tool"] = "TodoWrite"
            session["current_tool_args"] = ""
            session["phase"] = "running"
            session["outcome"] = ""
        elif event == "PreToolUse" and tool_name:
            session["last_tool"] = tool_name
            session["current_tool"] = tool_name
            session["current_tool_args"] = _tool_args_summary(tool_name, tool_input)
            session["phase"] = "running"
            session["outcome"] = ""
            # A plan hand-off / direct question blocks on the user with no
            # PostToolUse until they answer — record it as a pending approval.
            if tool_name in BLOCKING_TOOLS:
                session["pending"][pkey] = {
                    "tool": _display_tool(tool_name),
                    "ask": _ask_text(tool_name, tool_input),
                    "ts": now,
                }
        elif event == "PermissionRequest":
            # A permission dialog is about to render. Record the pending ask
            # ONLY — never a decision (detect-only). PreToolUse already set the
            # tool/phase, so leave those alone.
            session["pending"][pkey] = {
                "tool": _display_tool(tool_name),
                "ask": _ask_text(tool_name, tool_input),
                "ts": now,
            }
        elif event in ("PostToolUse", "PostToolUseFailure"):
            # The tool resolved (approved→ran, or ran→failed): clear its pending
            # entry by identity so a sibling subagent's prompt is untouched.
            # Keep current_tool set (avoids a mid-turn flicker to idle).
            session["pending"].pop(pkey, None)
            session["phase"] = "running"
            session["outcome"] = ""
        elif event == "PermissionDenied":
            session["pending"].pop(pkey, None)
        elif event == "UserPromptSubmit":
            prompt = payload.get("prompt", "")
            if isinstance(prompt, str):
                session["last_user_prompt"] = prompt[:120]
            _archive_current(session)
            session["phase"] = "running"
            session["outcome"] = ""
            session["current_tool"] = ""
            session["current_tool_args"] = ""
            session["pending"] = {}
        elif event == "Stop":
            _archive_current(session)
            session["last_tool"] = "idle"
            session["current_tool"] = ""
            session["current_tool_args"] = ""
            session["phase"] = "idle"
            session["outcome"] = "completed"
            session["pending"] = {}
        elif event == "StopFailure":
            _archive_current(session)
            session["current_tool"] = ""
            session["current_tool_args"] = ""
            session["last_tool"] = "idle"
            session["phase"] = "idle"
            session["outcome"] = "failed"
            session["error_type"] = str(payload.get("error_type") or "")[:40]
            session["pending"] = {}
        elif event == "SessionStart":
            # Resume / fresh open: idle until a tool fires.
            session["phase"] = "idle"
            session["current_tool"] = ""
            session["current_tool_args"] = ""
            session["outcome"] = ""
            session["pending"] = {}
            title = payload.get("session_title")
            if isinstance(title, str) and title.strip():
                session["session_title"] = title.strip()[:60]

        sessions[session_id] = session
        state["sessions"] = sessions

        tmp = STATE_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, separators=(",", ":")))
        os.replace(tmp, STATE_FILE)


def main() -> int:
    try:
        raw = sys.stdin.read()
        if not raw.strip():
            return 0
        payload = json.loads(raw)
        if isinstance(payload, dict):
            _update(payload)
    except Exception:
        # Fail-open — never block Claude Code on our errors.
        pass
    finally:
        # Empty JSON directive = no-op, let Claude proceed. This is the
        # structural detect-only guarantee: no decision is ever emitted.
        sys.stdout.write("{}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
