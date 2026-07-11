#pragma once
#include "data.h"
#include "ble.h"

enum screen_t {
    SCREEN_SPLASH,
    SCREEN_USAGE,
    SCREEN_ALERT,   // attention takeover (RED/AMBER) — NOT part of the tap ring
    SCREEN_COUNT,
};

void ui_init(void);
void ui_update(const UsageData* data);
void ui_set_working(bool working);
void ui_tick_anim(void);
void ui_show_screen(screen_t screen);
void ui_toggle_splash(void);
screen_t ui_get_current_screen(void);
void ui_update_ble_status(ble_state_t state, const char* name, const char* mac);
void ui_set_wifi_active(bool active);   // WiFi station is associated (a usable fallback link)
void ui_update_battery(int percent, bool charging);

// ---- Attention overlay (severity ladder: RED > AMBER > BLUE > calm) ----
// Cache the daemon's attention fields; call every loop iteration alongside
// ui_update(). link_stale = true when no payload has landed within
// LINK_STALE_MS (main.cpp owns that clock) — the watchdog against a frozen
// "all clear" screen.
void ui_set_attention(int blocked_count, int blocked_age, const char* block_project,
                      int idle_turn, int failed_count, bool link_stale);
// Drive the severity takeover/overlay + the amber breathing pulse. Call every
// loop iteration (cheap — no-ops most ticks).
void ui_attention_tick(void);
