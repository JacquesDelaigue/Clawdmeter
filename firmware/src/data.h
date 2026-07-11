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
    char host[40];           // daemon LAN address (BLE payload "host"), for WiFi fallback; "" if absent
    int  port;               // daemon HTTP port (BLE payload "port"); 0 if absent
};

#define MAX_SESSIONS 5

enum session_state_t : uint8_t {
    SESSION_WORKING = 0,
    SESSION_NEEDS_INPUT = 1,
    SESSION_COMPLETED = 2,
    SESSION_FAILED = 3,
};

struct SessionData {
    char project[24];   // short project name (cwd basename)
    char model[16];     // short model id, e.g. "opus-4-8"
    char effort[8];     // effort level, e.g. "xhigh"/"max"/"high"/"medium"/"low"
    int  ctx_pct;       // context-window utilization 0-100 (approximate)
    bool working;       // phase == "running"
    char id[5];          // stable session id (wire contract: <=4 chars)
    session_state_t state;  // needs_input/completed/failed lifecycle tag; independent of `working`
    char summary[36];       // row label shown on the Activity/Approval screens
    char approval_ask[48];  // pending-approval question; set when state == SESSION_NEEDS_INPUT
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
