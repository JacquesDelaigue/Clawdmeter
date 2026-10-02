#include "wifi_transport.h"

// Pull in user config if present. wifi_cfg.h is gitignored (copied from
// wifi_cfg.h.example); when it's absent the firmware still builds — WiFi is
// simply compiled out and every entry point is a no-op.
#if defined(__has_include)
#  if __has_include("wifi_cfg.h")
#    include "wifi_cfg.h"
#  endif
#endif

// ---- Config defaults (any not set by wifi_cfg.h) --------------------------
#ifndef WIFI_ENABLED
#  define WIFI_ENABLED 0          // no wifi_cfg.h → BLE-only build
#endif
#ifndef WIFI_SSID
#  define WIFI_SSID ""
#endif
#ifndef WIFI_PASS
#  define WIFI_PASS ""
#endif
#ifndef WIFI_TOKEN
#  define WIFI_TOKEN ""           // shared secret; must match the daemon. "" = no auth
#endif
#ifndef WIFI_HOST_FALLBACK
#  define WIFI_HOST_FALLBACK ""   // used before the daemon's address is learned over BLE
#endif
#ifndef WIFI_HOST_PORT
#  define WIFI_HOST_PORT 47800
#endif
#ifndef WIFI_POLL_MS
#  define WIFI_POLL_MS 5000       // how often to pull /usage once WiFi is the active source
#endif
#ifndef WIFI_TAKEOVER_MS
#  define WIFI_TAKEOVER_MS 20000  // only use WiFi after BLE data has been silent this long
#endif
#ifndef WIFI_RETRY_MS
#  define WIFI_RETRY_MS 15000     // re-issue WiFi.begin() this often while disconnected
#endif

// The desktop simulator (-e sim) has no WiFi stack: always use the no-op stubs
// there, even if a local wifi_cfg.h enables WiFi for the hardware builds.
#if defined(BOARD_SIM)
#  undef  WIFI_ENABLED
#  define WIFI_ENABLED 0
#endif

#if WIFI_ENABLED

#include <WiFi.h>
#include <HTTPClient.h>
#include <Preferences.h>

static char     g_buf[1024];
static volatile bool g_data_ready = false;

static uint32_t g_ble_last_ms   = 0;      // millis() of the last BLE-delivered payload
static uint32_t g_poll_last_ms  = 0;      // millis() of the last HTTP GET attempt
static uint32_t g_begin_last_ms = 0;      // millis() of the last WiFi.begin()
static bool     g_begun         = false;

// Daemon LAN address learned from the BLE payload, persisted in NVS so it
// survives reboots and DHCP churn (same "clawdmeter" namespace as brightness).
static char g_host[40] = {0};
static int  g_port     = WIFI_HOST_PORT;

static void load_host(void) {
    Preferences prefs;
    prefs.begin("clawdmeter", true);
    prefs.getString("mac_host", g_host, sizeof(g_host));
    g_port = prefs.getInt("mac_port", WIFI_HOST_PORT);
    prefs.end();
    if (!g_host[0] && WIFI_HOST_FALLBACK[0]) {
        strlcpy(g_host, WIFI_HOST_FALLBACK, sizeof(g_host));
    }
}

void wifi_init(void) {
    load_host();
    WiFi.persistent(false);     // don't thrash NVS with the WiFi stack's own creds
    WiFi.mode(WIFI_STA);
    WiFi.setAutoReconnect(true);
    WiFi.setSleep(true);        // modem-sleep: lets the single 2.4GHz radio share time with NimBLE
    Serial.printf("WiFi: enabled, SSID '%s', host '%s:%d'\n",
                  WIFI_SSID, g_host[0] ? g_host : "(unset)", g_port);
}

void wifi_note_ble_data(void) {
    g_ble_last_ms = millis();
}

void wifi_note_ble_host(const char* host, int port) {
    if (!host || !host[0] || port <= 0) return;
    if (strncmp(host, g_host, sizeof(g_host)) == 0 && port == g_port) return;  // unchanged
    strlcpy(g_host, host, sizeof(g_host));
    g_port = port;
    Preferences prefs;
    prefs.begin("clawdmeter", false);
    prefs.putString("mac_host", g_host);
    prefs.putInt("mac_port", g_port);
    prefs.end();
    Serial.printf("WiFi: learned daemon address %s:%d\n", g_host, g_port);
}

bool wifi_link_up(void) {
    return WiFi.status() == WL_CONNECTED;
}

bool wifi_has_data(void) {
    return g_data_ready;
}

const char* wifi_get_data(void) {
    g_data_ready = false;
    return g_buf;
}

static void do_poll(void) {
    if (!g_host[0]) return;  // no daemon address known yet

    char url[96];
    if (WIFI_TOKEN[0])
        snprintf(url, sizeof(url), "http://%s:%d/usage?token=%s", g_host, g_port, WIFI_TOKEN);
    else
        snprintf(url, sizeof(url), "http://%s:%d/usage", g_host, g_port);

    WiFiClient client;
    HTTPClient http;
    http.setConnectTimeout(1500);
    http.setTimeout(2000);
    if (!http.begin(client, url)) return;
    int code = http.GET();
    if (code == 200) {
        String body = http.getString();
        if (body.length() > 0 && body.length() < sizeof(g_buf)) {
            strlcpy(g_buf, body.c_str(), sizeof(g_buf));
            g_data_ready = true;
        }
    } else if (code > 0) {
        Serial.printf("WiFi: GET %s -> HTTP %d\n", url, code);
    }
    http.end();
}

void wifi_tick(void) {
    uint32_t now = millis();

    // Keep the station associated; re-issue begin() periodically while down.
    if (WiFi.status() != WL_CONNECTED) {
        if (!g_begun || (now - g_begin_last_ms) >= WIFI_RETRY_MS) {
            WiFi.begin(WIFI_SSID, WIFI_PASS);
            g_begin_last_ms = now;
            g_begun = true;
        }
        return;
    }

    // BLE is primary. Only pull over WiFi once BLE data has gone stale.
    bool ble_stale = (now - g_ble_last_ms) >= WIFI_TAKEOVER_MS;
    if (ble_stale && (now - g_poll_last_ms) >= WIFI_POLL_MS) {
        g_poll_last_ms = now;
        do_poll();
    }
}

#else  // WIFI_ENABLED == 0 — no-op stubs so the BLE-only build is unchanged

void wifi_init(void) {}
void wifi_tick(void) {}
bool wifi_has_data(void) { return false; }
const char* wifi_get_data(void) { return ""; }
bool wifi_link_up(void) { return false; }
void wifi_note_ble_data(void) {}
void wifi_note_ble_host(const char* host, int port) { (void)host; (void)port; }

#endif // WIFI_ENABLED
