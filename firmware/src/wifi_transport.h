#pragma once
#include <Arduino.h>

// WiFi transport — a fallback data path for when the device is out of BLE
// range of the host. It pulls the SAME JSON usage payload the daemon sends
// over BLE, from the daemon's small HTTP endpoint (GET /usage), and feeds it
// into the exact same parse/render path in main.cpp. BLE stays the primary
// link (lower latency, no network); WiFi only takes over once BLE data has
// gone stale (see WIFI_TAKEOVER_MS in wifi_cfg.h).
//
// Provisioning: WiFi SSID/password come from wifi_cfg.h (compile-time, copied
// from wifi_cfg.h.example and gitignored). The daemon's LAN address is learned
// automatically from the BLE payload ("host"/"port" fields) and cached in NVS,
// so it survives DHCP changes with no reflash; a compile-time fallback host can
// also be set for first boot before any BLE contact.
//
// When WIFI_ENABLED is 0 (no wifi_cfg.h present, or explicitly disabled) every
// function below is a no-op stub, so the firmware builds and behaves exactly as
// the BLE-only build.

void wifi_init(void);
void wifi_tick(void);             // call once per loop(); non-blocking except the periodic GET

bool wifi_has_data(void);         // true if a fresh payload arrived since the last wifi_get_data()
const char* wifi_get_data(void);  // returns the JSON buffer and clears the has-data flag

bool wifi_link_up(void);          // station associated to the AP (so the UI shows usage, not the pairing hint)

void wifi_note_ble_data(void);                     // call when BLE delivers a payload (resets the takeover timer)
void wifi_note_ble_host(const char* host, int port); // learn the daemon's LAN address from the BLE payload
