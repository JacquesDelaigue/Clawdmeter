#pragma once
#include <Arduino.h>

struct UsageData {
    float session_pct;       // utilization 0-100 (5h window Pro/Max; spending % Enterprise)
    int session_reset_mins;  // minutes until reset
    float weekly_pct;        // 7-day utilization (Pro/Max only; 0 for Enterprise)
    int weekly_reset_mins;   // minutes until weekly reset (Pro/Max only)
    char status[16];         // "allowed", "limited", etc.
    bool chime;              // play the session-reset chime; false unless daemon opts in
    bool enterprise;         // true = Enterprise spending-limit account
    int time_pct;            // 0-100: fraction of billing period elapsed (Enterprise)
    int period_days;         // total billing period length in days (Enterprise)
    char reset_date[12];     // formatted reset date e.g. "Jul 1" (Enterprise)
    long clock_epoch;        // local wall-clock epoch (s) from daemon; 0 = not provided
    int  clock_fmt;          // 12 or 24 (hour format from daemon); defaults to 24
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
