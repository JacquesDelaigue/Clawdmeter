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

    // ---- Attention fields (severity ladder — see ui_set_attention) ----
    int  blocked_count;      // sessions blocked on you: permission/plan/AskUserQuestion ("bc")
    int  blocked_age;        // age in seconds of the oldest block ("ba")
    char block_project[20];  // project name of the oldest block, <=16 chars ("bp")
    int  idle_turn;          // "your turn": sessions finished, waiting on you ("it")
    int  failed_count;       // sessions that hit StopFailure ("fc")
};
