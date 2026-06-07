#pragma once
#include <Arduino.h>

struct UsageData {
    float session_pct;       // 5-hour window utilization (0-100)
    int session_reset_mins;  // minutes until session resets
    float weekly_pct;        // 7-day window utilization (0-100)
    int weekly_reset_mins;   // minutes until weekly resets
    char status[16];         // "allowed" or "limited"
    bool ok;                 // data parse succeeded
    bool working;            // any Claude Code session is actively running (daemon "working")
    bool valid;              // false until first successful parse
};

#define MAX_SESSIONS 5

struct SessionData {
    char project[24];   // short project name (cwd basename)
    char model[16];     // short model id, e.g. "opus-4-8"
    char effort[8];     // effort level, e.g. "xhigh"/"max"/"high"/"medium"/"low"
    int  ctx_pct;       // context-window utilization 0-100 (approximate)
    bool working;       // phase == "running"
    // --- detail (best-effort; may be dropped by the daemon's byte budget) ---
    char activity[40];  // current activity headline, e.g. "Edit ui.cpp" / "Idle"
    int  todo_done;     // completed todos
    int  todo_total;    // total todos (0 = no todo list)
    char todo_now[40];  // the in-progress todo (activeForm), "" if none
    int  idle_secs;     // seconds since this session last did anything
    bool has_detail;    // false when the daemon dropped detail for this session
};

struct ActivityData {
    SessionData sessions[MAX_SESSIONS];
    int  count;         // number of valid sessions, 0..MAX_SESSIONS
    bool valid;         // false until first parse
};
