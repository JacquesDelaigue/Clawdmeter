#include "ui.h"
#include "splash.h"
#include <lvgl.h>
#include "logo.h"
#include "icons.h"
#include "hal/board_caps.h"

// Custom fonts (scaled for 314 PPI, ~1.9x from original 165 PPI)
LV_FONT_DECLARE(font_tiempos_56);
LV_FONT_DECLARE(font_tiempos_34);
LV_FONT_DECLARE(font_styrene_48);
LV_FONT_DECLARE(font_styrene_28);
LV_FONT_DECLARE(font_styrene_24);
LV_FONT_DECLARE(font_styrene_20);
LV_FONT_DECLARE(font_styrene_16);
LV_FONT_DECLARE(font_styrene_14);
LV_FONT_DECLARE(font_mono_32);

// Layout values computed from the active board's geometry. Populated once
// in ui_init() and treated as const for the rest of the program. Adding a
// new display size means extending compute_layout() with another
// breakpoint — never editing the screen-builder functions below.
struct Layout {
    int16_t scr_w, scr_h;
    int16_t margin;
    int16_t title_y;
    int16_t content_y;
    int16_t content_w;

    // Usage screen
    int16_t usage_panel_h;
    int16_t usage_panel_gap;
    int16_t usage_bar_y;
    int16_t usage_reset_y;

    // Bluetooth screen
    int16_t bt_info_panel_h;
    int16_t bt_reset_zone_h;
    const lv_font_t* bt_title_font;
    const lv_font_t* bt_status_font;
    const lv_font_t* bt_device_font;
    const lv_font_t* bt_credit_1_font;
    const lv_font_t* bt_credit_2_font;

    // Activity screen
    int16_t act_row_h;
    int16_t act_row_gap;
    const lv_font_t* act_proj_font;
    const lv_font_t* act_meta_font;
};
static Layout L = {};

// Pick layout values from the active board's pixel dimensions. The two
// existing boards happen to land on the two breakpoints below; new ports
// inherit the closer one — visually OK, may need a polish pass for
// pixel-perfect alignment but never blocks the port from booting.
static void compute_layout(const BoardCaps& c) {
    L.scr_w = c.width;
    L.scr_h = c.height;
    L.margin = 20;
    L.title_y = 30;

    if (c.height >= 460) {
        // Large layout — tuned for 480x480 (AMOLED-2.16).
        L.content_y = 100;
        L.usage_panel_h = 150;
        L.usage_panel_gap = 16;
        L.usage_bar_y = 56;
        L.usage_reset_y = 94;
        L.bt_info_panel_h = 160;
        L.bt_reset_zone_h = 110;
        L.bt_title_font    = &font_tiempos_56;
        L.bt_status_font   = &font_styrene_48;
        L.bt_device_font   = &font_styrene_28;
        L.bt_credit_1_font = &font_styrene_24;
        L.bt_credit_2_font = &font_styrene_20;
        L.act_row_h = 78;
        L.act_row_gap = 10;
        L.act_proj_font = &font_styrene_24;
        L.act_meta_font = &font_styrene_16;
    } else {
        // Compact layout — tuned for 368x448 (AMOLED-1.8).
        L.content_y = 85;
        L.usage_panel_h = 130;
        L.usage_panel_gap = 12;
        L.usage_bar_y = 48;
        L.usage_reset_y = 78;
        L.bt_info_panel_h = 140;
        L.bt_reset_zone_h = 90;
        L.bt_title_font    = &font_tiempos_34;
        L.bt_status_font   = &font_styrene_28;
        L.bt_device_font   = &font_styrene_20;
        L.bt_credit_1_font = &font_styrene_16;
        L.bt_credit_2_font = &font_styrene_14;
        L.act_row_h = 64;
        L.act_row_gap = 8;
        L.act_proj_font = &font_styrene_20;
        L.act_meta_font = &font_styrene_14;
    }

    L.content_w = L.scr_w - 2 * L.margin;
}

// Anthropic brand palette — design tokens live in theme.h
#include "theme.h"
#define COL_BG        THEME_BG
#define COL_PANEL     THEME_PANEL
#define COL_TEXT      THEME_TEXT
#define COL_DIM       THEME_DIM
#define COL_ACCENT    THEME_ACCENT
#define COL_GREEN     THEME_GREEN
#define COL_AMBER     THEME_AMBER
#define COL_RED       THEME_RED
#define COL_BAR_BG    THEME_BAR_BG

// ---- Usage screen widgets (single non-splash view) ----
static lv_obj_t* usage_container;
static lv_obj_t* lbl_title;
static lv_obj_t* usage_group;   // the two usage panels — shown when connected
static lv_obj_t* pair_group;    // pairing hint — shown when disconnected
static lv_obj_t* bar_session;
static lv_obj_t* lbl_session_pct;
static lv_obj_t* lbl_session_label;
static lv_obj_t* lbl_session_reset;
static lv_obj_t* bar_weekly;
static lv_obj_t* lbl_weekly_pct;
static lv_obj_t* lbl_weekly_label;
static lv_obj_t* lbl_weekly_reset;
static lv_obj_t* lbl_anim;      // status line: connection state + whimsical idle

// ---- Battery indicator (shared, on top) ----
static lv_obj_t* battery_img;
static lv_obj_t* logo_img;
static lv_image_dsc_t battery_dscs[5];  // empty, low, medium, full, charging

// ---- Live-data freshness → which usage sub-view to show ----
// usage panels when data is flowing, an idle "Zzz" screen when the host is
// connected but no usage update landed within DATA_FRESH_MS, the pairing hint
// when BLE is down. Re-evaluated every loop in ui_tick_anim().
static lv_obj_t* idle_group;            // the "Zzz" idle screen
static uint32_t  last_data_ms = 0;      // lv_tick when the last valid usage update landed
static bool      data_received = false; // any valid update since boot
static int       view_state = -1;       // -1 unknown / 0 pair / 1 idle / 2 usage
static const uint32_t DATA_FRESH_MS = 90000;  // usage counts as "live" within this window (daemon sends ~60s)

// Mini creatures: the idle "Zzz" sleeper, and a top-left "work coding" badge
// shown while Claude Code is actively running.
static splash_mini_t s_idle_mini = {};
static splash_mini_t s_work_mini = {};
static lv_obj_t* work_mini_canvas = nullptr;
static bool      s_working = false;

// ---- Activity screen widgets (scrolling list of live Claude Code sessions) ----
static lv_obj_t* activity_container = nullptr;
static lv_obj_t* activity_list = nullptr;       // scrollable flex column
static lv_obj_t* activity_empty = nullptr;      // "No active sessions" placeholder
static lv_obj_t* row_panel[MAX_SESSIONS];
static lv_obj_t* row_proj[MAX_SESSIONS];      // session name (top-left, title font)
static lv_obj_t* row_modelbig[MAX_SESSIONS];  // model e.g. "Opus 4.8" (top-right, title font)
static lv_obj_t* row_status[MAX_SESSIONS];    // "Running"/"idle 5m" (bottom-left)
static lv_obj_t* row_effort[MAX_SESSIONS];    // effort e.g. "xhigh" (bottom, left of %)
static lv_obj_t* row_bar[MAX_SESSIONS];
static lv_obj_t* row_pct[MAX_SESSIONS];

// ---- Session detail screen (drill-in from an Activity row) ----
static lv_obj_t* detail_container = nullptr;
static lv_obj_t* detail_proj = nullptr;     // header: project name
static lv_obj_t* detail_model = nullptr;    // header: model id
static lv_obj_t* detail_status = nullptr;   // header: "Running"/"Idle"
static lv_obj_t* detail_idle = nullptr;     // header: "idle 5m" / "active now"
static lv_obj_t* detail_ctx_bar = nullptr;
static lv_obj_t* detail_ctx_pct = nullptr;
static lv_obj_t* detail_act_panel = nullptr;  // "Doing" panel (detail-only)
static lv_obj_t* detail_activity = nullptr;
static lv_obj_t* detail_todo_panel = nullptr; // "To-dos" panel (detail-only)
static lv_obj_t* detail_todo_lbl = nullptr;
static lv_obj_t* detail_todo_bar = nullptr;
static lv_obj_t* detail_todo_now = nullptr;
static lv_obj_t* detail_unavail = nullptr;    // "Detail unavailable" note

static ActivityData s_activity_cache = {};   // latest activity data, for the detail screen
static int selected_session = -1;            // which row opened the detail screen

// ---- Shared ----
static lv_image_dsc_t logo_dsc;
static screen_t current_screen = SCREEN_USAGE;
static bool     s_ble_connected = false;   // cached BLE connection state
static uint32_t connected_at_ms = 0;       // when we last entered CONNECTED ("Connected" dwell)

// Animation state
static uint32_t anim_last_ms = 0;
static uint8_t anim_spinner_idx = 0;
static uint8_t anim_phase = 0;
static uint8_t anim_msg_idx = 0;
static uint32_t anim_msg_start = 0;
#define ANIM_MSG_MS     4000

static const char* const spinner_frames[] = {
    "\xC2\xB7", "\xE2\x9C\xBB", "\xE2\x9C\xBD",
    "\xE2\x9C\xB6", "\xE2\x9C\xB3", "\xE2\x9C\xA2",
};
#define SPINNER_COUNT 6
#define SPINNER_PHASES (2 * (SPINNER_COUNT - 1))  // 10: ping-pong 0..5..0

static const uint16_t spinner_ms[SPINNER_COUNT] = {
    260, 130, 130, 130, 130, 260,
};

static const char* const anim_messages[] = {
    "Accomplishing", "Elucidating", "Perusing",
    "Actioning", "Enchanting", "Philosophising",
    "Actualizing", "Envisioning", "Pondering",
    "Baking", "Finagling", "Pontificating",
    "Booping", "Flibbertigibbeting", "Processing",
    "Brewing", "Forging", "Puttering",
    "Calculating", "Forming", "Puzzling",
    "Cerebrating", "Frolicking", "Reticulating",
    "Channelling", "Generating", "Ruminating",
    "Churning", "Germinating", "Scheming",
    "Clauding", "Hatching", "Schlepping",
    "Coalescing", "Herding", "Shimmying",
    "Cogitating", "Honking", "Shucking",
    "Combobulating", "Hustling", "Simmering",
    "Computing", "Ideating", "Smooshing",
    "Concocting", "Imagining", "Spelunking",
    "Conjuring", "Incubating", "Spinning",
    "Considering", "Inferring", "Stewing",
    "Contemplating", "Jiving", "Sussing",
    "Cooking", "Manifesting", "Synthesizing",
    "Crafting", "Marinating", "Thinking",
    "Creating", "Meandering", "Tinkering",
    "Crunching", "Moseying", "Transmuting",
    "Deciphering", "Mulling", "Unfurling",
    "Deliberating", "Mustering", "Unravelling",
    "Determining", "Musing", "Vibing",
    "Discombobulating", "Noodling", "Wandering",
    "Divining", "Percolating", "Whirring",
    "Doing", "Wibbling",
    "Effecting", "Wizarding",
    "Working", "Wrangling",
};
#define ANIM_MSG_COUNT (sizeof(anim_messages) / sizeof(anim_messages[0]))

static lv_color_t pct_color(float pct) {
    if (pct >= 80.0f) return COL_RED;
    if (pct >= 50.0f) return COL_AMBER;
    return COL_GREEN;
}

static void format_reset_time(int mins, char* buf, size_t len) {
    if (mins < 0) {
        snprintf(buf, len, "---");
    } else if (mins < 60) {
        snprintf(buf, len, "Resets in %dm", mins);
    } else if (mins < 1440) {
        snprintf(buf, len, "Resets in %dh %dm", mins / 60, mins % 60);
    } else {
        snprintf(buf, len, "Resets in %dd %dh", mins / 1440, (mins % 1440) / 60);
    }
}

// Forward decls — callbacks defined near ui_show_screen below
static void global_click_cb(lv_event_t* e);
static void row_click_cb(lv_event_t* e);
static void detail_back_cb(lv_event_t* e);

// Pretty-print a short model id for display: "opus-4-8" -> "Opus 4.8",
// "sonnet-4-6" -> "Sonnet 4.6". Family capitalized; version dashes -> dots.
static void format_model(const char* m, char* out, size_t n) {
    if (!m || !m[0]) { if (n) out[0] = '\0'; return; }
    char fam[16] = {0}, ver[16] = {0};
    int i = 0;
    while (m[i] && m[i] != '-' && i < (int)sizeof(fam) - 1) { fam[i] = m[i]; i++; }
    fam[i] = '\0';
    if (m[i] == '-') i++;
    int j = 0;
    while (m[i] && j < (int)sizeof(ver) - 1) { ver[j++] = (m[i] == '-') ? '.' : m[i]; i++; }
    ver[j] = '\0';
    if (fam[0] >= 'a' && fam[0] <= 'z') fam[0] -= 32;  // capitalize family
    if (ver[0]) snprintf(out, n, "%s %s", fam, ver);
    else        snprintf(out, n, "%s", fam);
}

// Format a "time since last activity" duration compactly: "12s" / "5m" / "1h 3m".
static void fmt_idle(int secs, char* buf, size_t n) {
    if (secs < 0) secs = 0;
    if (secs < 60)        snprintf(buf, n, "%ds", secs);
    else if (secs < 3600) snprintf(buf, n, "%dm", secs / 60);
    else                  snprintf(buf, n, "%dh %dm", secs / 3600, (secs % 3600) / 60);
}

static lv_obj_t* make_panel(lv_obj_t* parent, int x, int y, int w, int h) {
    lv_obj_t* panel = lv_obj_create(parent);
    lv_obj_set_pos(panel, x, y);
    lv_obj_set_size(panel, w, h);
    lv_obj_set_style_bg_color(panel, COL_PANEL, 0);
    lv_obj_set_style_bg_opa(panel, LV_OPA_COVER, 0);
    lv_obj_set_style_radius(panel, 8, 0);
    lv_obj_set_style_border_width(panel, 0, 0);
    lv_obj_set_style_pad_left(panel, 16, 0);
    lv_obj_set_style_pad_right(panel, 16, 0);
    lv_obj_set_style_pad_top(panel, 12, 0);
    lv_obj_set_style_pad_bottom(panel, 12, 0);
    lv_obj_clear_flag(panel, LV_OBJ_FLAG_SCROLLABLE);
    lv_obj_add_flag(panel, LV_OBJ_FLAG_EVENT_BUBBLE);
    return panel;
}

static lv_obj_t* make_bar(lv_obj_t* parent, int x, int y, int w, int h) {
    lv_obj_t* bar = lv_bar_create(parent);
    lv_obj_set_pos(bar, x, y);
    lv_obj_set_size(bar, w, h);
    lv_bar_set_range(bar, 0, 100);
    lv_bar_set_value(bar, 0, LV_ANIM_OFF);
    lv_obj_set_style_bg_color(bar, COL_BAR_BG, LV_PART_MAIN);
    lv_obj_set_style_bg_opa(bar, LV_OPA_COVER, LV_PART_MAIN);
    lv_obj_set_style_radius(bar, 6, LV_PART_MAIN);
    lv_obj_set_style_bg_color(bar, COL_GREEN, LV_PART_INDICATOR);
    lv_obj_set_style_bg_opa(bar, LV_OPA_COVER, LV_PART_INDICATOR);
    lv_obj_set_style_radius(bar, 6, LV_PART_INDICATOR);
    return bar;
}

static void init_icon_dsc_rgb565a8(lv_image_dsc_t* dsc, int w, int h, const uint8_t* data) {
    dsc->header.w = w;
    dsc->header.h = h;
    dsc->header.cf = LV_COLOR_FORMAT_RGB565A8;
    dsc->header.stride = w * 2;
    dsc->data = data;
    dsc->data_size = w * h * 3;
}

static lv_obj_t* make_pill(lv_obj_t* parent, const char* text) {
    lv_obj_t* lbl = lv_label_create(parent);
    lv_label_set_text(lbl, text);
    lv_obj_set_style_text_font(lbl, &font_styrene_28, 0);
    lv_obj_set_style_text_color(lbl, COL_TEXT, 0);
    lv_obj_set_style_bg_color(lbl, COL_BAR_BG, 0);
    lv_obj_set_style_bg_opa(lbl, LV_OPA_COVER, 0);
    lv_obj_set_style_radius(lbl, LV_RADIUS_CIRCLE, 0);
    lv_obj_set_style_pad_left(lbl, 18, 0);
    lv_obj_set_style_pad_right(lbl, 18, 0);
    lv_obj_set_style_pad_top(lbl, 6, 0);
    lv_obj_set_style_pad_bottom(lbl, 6, 0);
    return lbl;
}

static void init_battery_icons(void) {
    init_icon_dsc_rgb565a8(&battery_dscs[0], ICON_BATTERY_W, ICON_BATTERY_H, icon_battery_data);
    init_icon_dsc_rgb565a8(&battery_dscs[1], ICON_BATTERY_LOW_W, ICON_BATTERY_LOW_H, icon_battery_low_data);
    init_icon_dsc_rgb565a8(&battery_dscs[2], ICON_BATTERY_MEDIUM_W, ICON_BATTERY_MEDIUM_H, icon_battery_medium_data);
    init_icon_dsc_rgb565a8(&battery_dscs[3], ICON_BATTERY_FULL_W, ICON_BATTERY_FULL_H, icon_battery_full_data);
    init_icon_dsc_rgb565a8(&battery_dscs[4], ICON_BATTERY_CHARGING_W, ICON_BATTERY_CHARGING_H, icon_battery_charging_data);
}

// ======== Usage Screen ========

static void make_usage_panel(lv_obj_t* parent, int y, const char* pill_text,
                             lv_obj_t** out_pct, lv_obj_t** out_pill,
                             lv_obj_t** out_bar, lv_obj_t** out_reset) {
    lv_obj_t* panel = make_panel(parent, L.margin, y, L.content_w, L.usage_panel_h);

    *out_pct = lv_label_create(panel);
    lv_label_set_text(*out_pct, "---%");
    lv_obj_set_style_text_font(*out_pct, &font_styrene_48, 0);
    lv_obj_set_style_text_color(*out_pct, COL_TEXT, 0);
    lv_obj_set_pos(*out_pct, 0, 0);

    *out_pill = make_pill(panel, pill_text);
    lv_obj_align(*out_pill, LV_ALIGN_TOP_RIGHT, 0, 1);

    *out_bar = make_bar(panel, 0, L.usage_bar_y, L.content_w - 32, 24);

    *out_reset = lv_label_create(panel);
    lv_label_set_text(*out_reset, "---");
    lv_obj_set_style_text_font(*out_reset, &font_styrene_28, 0);
    lv_obj_set_style_text_color(*out_reset, COL_DIM, 0);
    lv_obj_set_pos(*out_reset, 0, L.usage_reset_y);
}

// Pairing hint — shown when disconnected so the screen isn't empty and the
// user knows how to (re)pair. Wording matches the 3-second release gesture.
static void build_pair_group(lv_obj_t* parent) {
    pair_group = lv_obj_create(parent);
    lv_obj_set_size(pair_group, L.scr_w, L.scr_h - L.content_y);
    lv_obj_set_pos(pair_group, 0, L.content_y);
    lv_obj_set_style_bg_opa(pair_group, LV_OPA_TRANSP, 0);
    lv_obj_set_style_border_width(pair_group, 0, 0);
    lv_obj_set_style_pad_all(pair_group, 0, 0);
    lv_obj_clear_flag(pair_group, LV_OBJ_FLAG_SCROLLABLE);
    lv_obj_add_flag(pair_group, LV_OBJ_FLAG_EVENT_BUBBLE);

    lv_obj_t* l1 = lv_label_create(pair_group);
    lv_label_set_text(l1, "To pair");
    lv_obj_set_style_text_font(l1, L.bt_status_font, 0);
    lv_obj_set_style_text_color(l1, COL_TEXT, 0);
    lv_obj_align(l1, LV_ALIGN_TOP_MID, 0, 40);

    lv_obj_t* l2 = lv_label_create(pair_group);
    lv_label_set_text(l2, "hold the power button");
    lv_obj_set_style_text_font(l2, L.bt_device_font, 0);
    lv_obj_set_style_text_color(l2, COL_DIM, 0);
    lv_obj_align(l2, LV_ALIGN_TOP_MID, 0, 120);

    lv_obj_t* l3 = lv_label_create(pair_group);
    lv_label_set_text(l3, "for 3 seconds, then release");
    lv_obj_set_style_text_font(l3, L.bt_device_font, 0);
    lv_obj_set_style_text_color(l3, COL_DIM, 0);
    lv_obj_align(l3, LV_ALIGN_TOP_MID, 0, 160);

    lv_obj_add_flag(pair_group, LV_OBJ_FLAG_HIDDEN);  // ui_update_ble_status decides
}

// Idle "Zzz" screen — shown when the host is connected but no usage update has
// landed recently (token expired, daemon down, host asleep…). Full-screen, like
// the pairing hint, so we never render hours-old numbers as if they were live.
static void build_idle_group(lv_obj_t* parent) {
    idle_group = lv_obj_create(parent);
    lv_obj_set_size(idle_group, L.scr_w, L.scr_h - L.content_y);
    lv_obj_set_pos(idle_group, 0, L.content_y);
    lv_obj_set_style_bg_opa(idle_group, LV_OPA_TRANSP, 0);
    lv_obj_set_style_border_width(idle_group, 0, 0);
    lv_obj_set_style_pad_all(idle_group, 0, 0);
    lv_obj_clear_flag(idle_group, LV_OBJ_FLAG_SCROLLABLE);
    lv_obj_add_flag(idle_group, LV_OBJ_FLAG_EVENT_BUBBLE);

    // A shrunk-down sleeping creature (reused claudepix "expression sleep" art)
    // sits between the header and the status line; the animated "Listening…"
    // status line carries the words, so no extra text is needed here.
    lv_obj_t* creature = splash_mini_create(&s_idle_mini, idle_group, "expression sleep", 160);
    if (creature) lv_obj_align(creature, LV_ALIGN_CENTER, 0, -20);

    lv_obj_add_flag(idle_group, LV_OBJ_FLAG_HIDDEN);  // update_view_state decides
}

static void init_usage_screen(lv_obj_t* scr) {
    usage_container = lv_obj_create(scr);
    lv_obj_set_size(usage_container, L.scr_w, L.scr_h);
    lv_obj_set_pos(usage_container, 0, 0);
    lv_obj_set_style_bg_opa(usage_container, LV_OPA_TRANSP, 0);
    lv_obj_set_style_border_width(usage_container, 0, 0);
    lv_obj_set_style_pad_all(usage_container, 0, 0);
    lv_obj_clear_flag(usage_container, LV_OBJ_FLAG_SCROLLABLE);
    lv_obj_add_event_cb(usage_container, global_click_cb, LV_EVENT_CLICKED, NULL);

    lbl_title = lv_label_create(usage_container);
    lv_label_set_text(lbl_title, "Usage");
    lv_obj_set_style_text_font(lbl_title, &font_tiempos_56, 0);
    lv_obj_set_style_text_color(lbl_title, COL_TEXT, 0);
    lv_obj_align(lbl_title, LV_ALIGN_TOP_MID, 16, L.title_y);

    // Usage panels (shown when connected) live in a transparent full-size group
    // so they can be toggled against the pairing hint as one unit.
    usage_group = lv_obj_create(usage_container);
    lv_obj_set_size(usage_group, L.scr_w, L.scr_h);
    lv_obj_set_pos(usage_group, 0, 0);
    lv_obj_set_style_bg_opa(usage_group, LV_OPA_TRANSP, 0);
    lv_obj_set_style_border_width(usage_group, 0, 0);
    lv_obj_set_style_pad_all(usage_group, 0, 0);
    lv_obj_clear_flag(usage_group, LV_OBJ_FLAG_SCROLLABLE);
    lv_obj_add_flag(usage_group, LV_OBJ_FLAG_EVENT_BUBBLE);

    make_usage_panel(usage_group, L.content_y, "Current",
                     &lbl_session_pct, &lbl_session_label,
                     &bar_session, &lbl_session_reset);
    make_usage_panel(usage_group,
                     L.content_y + L.usage_panel_h + L.usage_panel_gap, "Weekly",
                     &lbl_weekly_pct, &lbl_weekly_label,
                     &bar_weekly, &lbl_weekly_reset);

    build_pair_group(usage_container);
    build_idle_group(usage_container);

    // Status line — always visible on the usage view. Driven by ui_tick_anim().
    lbl_anim = lv_label_create(usage_container);
    lv_label_set_text(lbl_anim, "");
    lv_obj_set_style_text_font(lbl_anim, &font_mono_32, 0);
    lv_obj_set_style_text_color(lbl_anim, COL_ACCENT, 0);
    lv_obj_align(lbl_anim, LV_ALIGN_BOTTOM_MID, 0, -15);

    // Top-left "working" badge: a small "work coding" Clawd shown only while
    // Claude Code is actively running (swaps in for the logo). Hidden by default.
    work_mini_canvas = splash_mini_create(&s_work_mini, usage_container, "work coding", 60);
    if (work_mini_canvas) {
        lv_obj_set_pos(work_mini_canvas, L.margin, L.title_y - 10);
        lv_obj_add_flag(work_mini_canvas, LV_OBJ_FLAG_HIDDEN);
    }
}

// ======== Activity Screen ========
// A vertical scrolling list of the user's live Claude Code sessions. Each row:
// project (top-left) · orange dot when running (top-right) · model + context-%
// bar + NN% (bottom). Tap cycles screens; drag scrolls the list.

static void build_activity_screen(lv_obj_t* scr) {
    activity_container = lv_obj_create(scr);
    lv_obj_set_size(activity_container, L.scr_w, L.scr_h);
    lv_obj_set_pos(activity_container, 0, 0);
    lv_obj_set_style_bg_opa(activity_container, LV_OPA_TRANSP, 0);
    lv_obj_set_style_border_width(activity_container, 0, 0);
    lv_obj_set_style_pad_all(activity_container, 0, 0);
    lv_obj_clear_flag(activity_container, LV_OBJ_FLAG_SCROLLABLE);
    lv_obj_add_event_cb(activity_container, global_click_cb, LV_EVENT_CLICKED, NULL);

    lv_obj_t* title = lv_label_create(activity_container);
    lv_label_set_text(title, "Activity");
    lv_obj_set_style_text_font(title, L.bt_title_font, 0);
    lv_obj_set_style_text_color(title, COL_TEXT, 0);
    lv_obj_align(title, LV_ALIGN_TOP_MID, 16, L.title_y);

    // Scrollable vertical list. Children stack via flex; hidden rows collapse.
    activity_list = lv_obj_create(activity_container);
    lv_obj_set_pos(activity_list, 0, L.content_y);
    lv_obj_set_size(activity_list, L.scr_w, L.scr_h - L.content_y);
    lv_obj_set_style_bg_opa(activity_list, LV_OPA_TRANSP, 0);
    lv_obj_set_style_border_width(activity_list, 0, 0);
    lv_obj_set_style_pad_left(activity_list, L.margin, 0);
    lv_obj_set_style_pad_right(activity_list, L.margin, 0);
    lv_obj_set_style_pad_top(activity_list, 0, 0);
    lv_obj_set_style_pad_bottom(activity_list, L.margin, 0);
    lv_obj_set_style_pad_row(activity_list, L.act_row_gap, 0);
    lv_obj_set_flex_flow(activity_list, LV_FLEX_FLOW_COLUMN);
    lv_obj_set_scroll_dir(activity_list, LV_DIR_VER);
    lv_obj_set_scrollbar_mode(activity_list, LV_SCROLLBAR_MODE_AUTO);
    lv_obj_add_flag(activity_list, LV_OBJ_FLAG_SCROLLABLE);
    lv_obj_add_flag(activity_list, LV_OBJ_FLAG_EVENT_BUBBLE);

    const int inner_w = L.content_w - 32;  // row inner width (panel has 16px L/R pad)

    for (int i = 0; i < MAX_SESSIONS; i++) {
        lv_obj_t* p = make_panel(activity_list, 0, 0, L.content_w, L.act_row_h);
        // A row tap opens that session's detail screen. make_panel sets
        // EVENT_BUBBLE (which would bubble the tap up to the screen-cycle
        // handler) — clear it so a row tap only fires row_click_cb. Scrolling
        // still works (it's driven by the scrollable list, not by bubbling).
        lv_obj_clear_flag(p, LV_OBJ_FLAG_EVENT_BUBBLE);
        lv_obj_add_event_cb(p, row_click_cb, LV_EVENT_CLICKED, (void*)(intptr_t)i);

        // --- top line: session name (left) + model (right), both title font ---
        row_proj[i] = lv_label_create(p);
        lv_label_set_long_mode(row_proj[i], LV_LABEL_LONG_DOT);
        lv_obj_set_width(row_proj[i], (int)(inner_w * 0.52f));
        lv_obj_set_style_text_font(row_proj[i], L.act_proj_font, 0);
        lv_obj_set_style_text_color(row_proj[i], COL_TEXT, 0);
        lv_label_set_text(row_proj[i], "");
        lv_obj_align(row_proj[i], LV_ALIGN_TOP_LEFT, 0, 0);

        row_modelbig[i] = lv_label_create(p);
        lv_label_set_long_mode(row_modelbig[i], LV_LABEL_LONG_DOT);
        lv_obj_set_width(row_modelbig[i], (int)(inner_w * 0.46f));
        lv_obj_set_style_text_align(row_modelbig[i], LV_TEXT_ALIGN_RIGHT, 0);
        lv_obj_set_style_text_font(row_modelbig[i], L.act_proj_font, 0);
        lv_obj_set_style_text_color(row_modelbig[i], COL_TEXT, 0);
        lv_label_set_text(row_modelbig[i], "");
        lv_obj_align(row_modelbig[i], LV_ALIGN_TOP_RIGHT, 0, 0);

        // --- bottom line: status (left) · effort · % · bar (right) ---
        row_bar[i] = make_bar(p, 0, 0, (int)(inner_w * 0.36f), 10);
        lv_obj_align(row_bar[i], LV_ALIGN_BOTTOM_RIGHT, 0, -3);

        row_pct[i] = lv_label_create(p);
        lv_obj_set_width(row_pct[i], 52);  // fixed width + right-align so it never overlaps the bar
        lv_obj_set_style_text_align(row_pct[i], LV_TEXT_ALIGN_RIGHT, 0);
        lv_obj_set_style_text_font(row_pct[i], L.act_meta_font, 0);
        lv_obj_set_style_text_color(row_pct[i], COL_TEXT, 0);
        lv_label_set_text(row_pct[i], "");
        lv_obj_align_to(row_pct[i], row_bar[i], LV_ALIGN_OUT_LEFT_MID, -6, 0);

        row_effort[i] = lv_label_create(p);
        lv_obj_set_width(row_effort[i], 64);
        lv_obj_set_style_text_align(row_effort[i], LV_TEXT_ALIGN_RIGHT, 0);
        lv_obj_set_style_text_font(row_effort[i], L.act_meta_font, 0);
        lv_obj_set_style_text_color(row_effort[i], COL_DIM, 0);
        lv_label_set_text(row_effort[i], "");
        lv_obj_align_to(row_effort[i], row_pct[i], LV_ALIGN_OUT_LEFT_MID, -8, 0);

        row_status[i] = lv_label_create(p);
        lv_label_set_long_mode(row_status[i], LV_LABEL_LONG_DOT);
        // Width = whatever's left after the right-side cluster (bar+%+effort),
        // so the status text can never overlap it on a narrow board.
        int status_w = inner_w - (int)(inner_w * 0.36f) - 52 - 64 - 24;
        if (status_w < 40) status_w = 40;
        lv_obj_set_width(row_status[i], status_w);
        lv_obj_set_style_text_font(row_status[i], L.act_meta_font, 0);
        lv_obj_set_style_text_color(row_status[i], COL_DIM, 0);
        lv_label_set_text(row_status[i], "");
        lv_obj_align(row_status[i], LV_ALIGN_BOTTOM_LEFT, 0, 0);

        lv_obj_add_flag(p, LV_OBJ_FLAG_HIDDEN);
        row_panel[i] = p;
    }

    activity_empty = lv_label_create(activity_container);
    lv_label_set_text(activity_empty, "No active sessions");
    lv_obj_set_style_text_font(activity_empty, L.bt_device_font, 0);
    lv_obj_set_style_text_color(activity_empty, COL_DIM, 0);
    lv_obj_align(activity_empty, LV_ALIGN_CENTER, 0, 0);
    lv_obj_add_flag(activity_empty, LV_OBJ_FLAG_HIDDEN);

    lv_obj_add_flag(activity_container, LV_OBJ_FLAG_HIDDEN);
}

// ======== Session Detail Screen ========
// Drill-in from an Activity row. Shows everything we have on one session:
// project + model + running/idle + idle-time (header), context-% (bar), the
// current activity ("Doing"), and to-do progress. Tap anywhere → back to list.

static void build_session_detail_screen(lv_obj_t* scr) {
    detail_container = lv_obj_create(scr);
    lv_obj_set_size(detail_container, L.scr_w, L.scr_h);
    lv_obj_set_pos(detail_container, 0, 0);
    lv_obj_set_style_bg_opa(detail_container, LV_OPA_TRANSP, 0);
    lv_obj_set_style_border_width(detail_container, 0, 0);
    lv_obj_set_style_pad_all(detail_container, 0, 0);
    lv_obj_clear_flag(detail_container, LV_OBJ_FLAG_SCROLLABLE);
    lv_obj_add_event_cb(detail_container, detail_back_cb, LV_EVENT_CLICKED, NULL);

    // Vertical stack of cards below the logo/battery zone.
    lv_obj_t* col = lv_obj_create(detail_container);
    lv_obj_set_pos(col, 0, L.content_y);
    lv_obj_set_size(col, L.scr_w, L.scr_h - L.content_y);
    lv_obj_set_style_bg_opa(col, LV_OPA_TRANSP, 0);
    lv_obj_set_style_border_width(col, 0, 0);
    lv_obj_set_style_pad_left(col, L.margin, 0);
    lv_obj_set_style_pad_right(col, L.margin, 0);
    lv_obj_set_style_pad_top(col, 0, 0);
    lv_obj_set_style_pad_bottom(col, L.margin, 0);
    lv_obj_set_style_pad_row(col, L.act_row_gap, 0);
    lv_obj_set_flex_flow(col, LV_FLEX_FLOW_COLUMN);
    lv_obj_set_scroll_dir(col, LV_DIR_VER);
    lv_obj_set_scrollbar_mode(col, LV_SCROLLBAR_MODE_AUTO);
    lv_obj_add_flag(col, LV_OBJ_FLAG_EVENT_BUBBLE);

    const int inner_w = L.content_w - 32;  // inside a panel's 16px L/R padding

    // --- Header card: project + status / model + idle-time ---
    lv_obj_t* hdr = make_panel(col, 0, 0, L.content_w, L.act_row_h);

    detail_proj = lv_label_create(hdr);
    lv_label_set_long_mode(detail_proj, LV_LABEL_LONG_DOT);
    lv_obj_set_width(detail_proj, inner_w - 90);
    lv_obj_set_style_text_font(detail_proj, L.act_proj_font, 0);
    lv_obj_set_style_text_color(detail_proj, COL_TEXT, 0);
    lv_label_set_text(detail_proj, "");
    lv_obj_align(detail_proj, LV_ALIGN_TOP_LEFT, 0, 0);

    detail_status = lv_label_create(hdr);
    lv_obj_set_style_text_font(detail_status, L.act_meta_font, 0);
    lv_obj_set_style_text_color(detail_status, COL_DIM, 0);
    lv_label_set_text(detail_status, "");
    lv_obj_align(detail_status, LV_ALIGN_TOP_RIGHT, 0, 2);

    detail_model = lv_label_create(hdr);
    lv_obj_set_style_text_font(detail_model, L.act_meta_font, 0);
    lv_obj_set_style_text_color(detail_model, COL_DIM, 0);
    lv_label_set_text(detail_model, "");
    lv_obj_align(detail_model, LV_ALIGN_BOTTOM_LEFT, 0, 0);

    detail_idle = lv_label_create(hdr);
    lv_obj_set_style_text_font(detail_idle, L.act_meta_font, 0);
    lv_obj_set_style_text_color(detail_idle, COL_DIM, 0);
    lv_label_set_text(detail_idle, "");
    lv_obj_align(detail_idle, LV_ALIGN_BOTTOM_RIGHT, 0, 0);

    // --- Context card: caption + NN% / full-width bar ---
    lv_obj_t* ctx = make_panel(col, 0, 0, L.content_w, L.act_row_h);

    lv_obj_t* ctx_cap = lv_label_create(ctx);
    lv_label_set_text(ctx_cap, "Context");
    lv_obj_set_style_text_font(ctx_cap, L.act_meta_font, 0);
    lv_obj_set_style_text_color(ctx_cap, COL_DIM, 0);
    lv_obj_align(ctx_cap, LV_ALIGN_TOP_LEFT, 0, 0);

    detail_ctx_pct = lv_label_create(ctx);
    lv_obj_set_style_text_font(detail_ctx_pct, L.act_meta_font, 0);
    lv_obj_set_style_text_color(detail_ctx_pct, COL_TEXT, 0);
    lv_label_set_text(detail_ctx_pct, "");
    lv_obj_align(detail_ctx_pct, LV_ALIGN_TOP_RIGHT, 0, 0);

    detail_ctx_bar = make_bar(ctx, 0, 0, inner_w, 12);
    lv_obj_align(detail_ctx_bar, LV_ALIGN_BOTTOM_LEFT, 0, -2);

    // --- "Doing" card: current activity (detail-only) ---
    detail_act_panel = make_panel(col, 0, 0, L.content_w, L.act_row_h + 8);

    lv_obj_t* act_cap = lv_label_create(detail_act_panel);
    lv_label_set_text(act_cap, "Doing");
    lv_obj_set_style_text_font(act_cap, L.act_meta_font, 0);
    lv_obj_set_style_text_color(act_cap, COL_DIM, 0);
    lv_obj_align(act_cap, LV_ALIGN_TOP_LEFT, 0, 0);

    detail_activity = lv_label_create(detail_act_panel);
    lv_label_set_long_mode(detail_activity, LV_LABEL_LONG_DOT);
    lv_obj_set_width(detail_activity, inner_w);
    lv_obj_set_style_text_font(detail_activity, L.act_proj_font, 0);
    lv_obj_set_style_text_color(detail_activity, COL_TEXT, 0);
    lv_label_set_text(detail_activity, "");
    lv_obj_align(detail_activity, LV_ALIGN_BOTTOM_LEFT, 0, 0);

    // --- To-dos card: "N/M" + progress bar + in-progress item (detail-only) ---
    detail_todo_panel = make_panel(col, 0, 0, L.content_w, L.act_row_h + 18);

    detail_todo_lbl = lv_label_create(detail_todo_panel);
    lv_label_set_text(detail_todo_lbl, "To-dos");
    lv_obj_set_style_text_font(detail_todo_lbl, L.act_meta_font, 0);
    lv_obj_set_style_text_color(detail_todo_lbl, COL_TEXT, 0);
    lv_obj_align(detail_todo_lbl, LV_ALIGN_TOP_LEFT, 0, 0);

    detail_todo_bar = make_bar(detail_todo_panel, 0, 0, inner_w, 10);
    lv_obj_set_style_bg_color(detail_todo_bar, COL_ACCENT, LV_PART_INDICATOR);
    lv_obj_align(detail_todo_bar, LV_ALIGN_CENTER, 0, 0);

    detail_todo_now = lv_label_create(detail_todo_panel);
    lv_label_set_long_mode(detail_todo_now, LV_LABEL_LONG_DOT);
    lv_obj_set_width(detail_todo_now, inner_w);
    lv_obj_set_style_text_font(detail_todo_now, L.act_meta_font, 0);
    lv_obj_set_style_text_color(detail_todo_now, COL_DIM, 0);
    lv_label_set_text(detail_todo_now, "");
    lv_obj_align(detail_todo_now, LV_ALIGN_BOTTOM_LEFT, 0, 0);

    // Shown instead of the Doing/To-dos cards when the daemon dropped detail
    // for this session to fit the BLE payload.
    detail_unavail = lv_label_create(col);
    lv_label_set_long_mode(detail_unavail, LV_LABEL_LONG_WRAP);
    lv_obj_set_width(detail_unavail, L.content_w);
    lv_label_set_text(detail_unavail, "Detail unavailable\n(too many active sessions)");
    lv_obj_set_style_text_font(detail_unavail, L.act_meta_font, 0);
    lv_obj_set_style_text_color(detail_unavail, COL_DIM, 0);
    lv_obj_set_style_text_align(detail_unavail, LV_TEXT_ALIGN_CENTER, 0);
    lv_obj_add_flag(detail_unavail, LV_OBJ_FLAG_HIDDEN);

    lv_obj_add_flag(detail_container, LV_OBJ_FLAG_HIDDEN);
}

void ui_update_session_detail(const SessionData* s) {
    if (!detail_container || !s) return;

    lv_label_set_text(detail_proj, s->project[0] ? s->project : "(session)");
    char mbuf[20];
    format_model(s->model, mbuf, sizeof(mbuf));
    if (s->effort[0]) {
        char ml[32];
        snprintf(ml, sizeof(ml), "%s (%s)", mbuf, s->effort);  // "Opus 4.8 (xhigh)"
        lv_label_set_text(detail_model, ml);
    } else {
        lv_label_set_text(detail_model, mbuf);
    }

    if (s->working) {
        lv_label_set_text(detail_status, "Running");
        lv_obj_set_style_text_color(detail_status, COL_ACCENT, 0);
        lv_label_set_text(detail_idle, "active now");
    } else {
        lv_label_set_text(detail_status, "Idle");
        lv_obj_set_style_text_color(detail_status, COL_DIM, 0);
        char t[16], ibuf[24];
        fmt_idle(s->idle_secs, t, sizeof(t));
        snprintf(ibuf, sizeof(ibuf), "idle %s", t);
        lv_label_set_text(detail_idle, ibuf);
    }

    int p = s->ctx_pct; if (p < 0) p = 0; if (p > 100) p = 100;
    lv_bar_set_value(detail_ctx_bar, p, LV_ANIM_OFF);
    lv_obj_set_style_bg_color(detail_ctx_bar, pct_color((float)p), LV_PART_INDICATOR);
    lv_label_set_text_fmt(detail_ctx_pct, "%d%%", p);

    if (s->has_detail) {
        lv_obj_clear_flag(detail_act_panel, LV_OBJ_FLAG_HIDDEN);
        lv_obj_clear_flag(detail_todo_panel, LV_OBJ_FLAG_HIDDEN);
        lv_obj_add_flag(detail_unavail, LV_OBJ_FLAG_HIDDEN);

        lv_label_set_text(detail_activity, s->activity[0] ? s->activity : "Idle");

        if (s->todo_total > 0) {
            lv_label_set_text_fmt(detail_todo_lbl, "To-dos  %d/%d", s->todo_done, s->todo_total);
            lv_bar_set_value(detail_todo_bar, 100 * s->todo_done / s->todo_total, LV_ANIM_OFF);
            lv_obj_clear_flag(detail_todo_bar, LV_OBJ_FLAG_HIDDEN);
            // The in-progress item, marked active by brand-orange text (the
            // Styrene font has no arrow glyph to prefix it with).
            lv_label_set_text(detail_todo_now, s->todo_now);
            lv_obj_set_style_text_color(detail_todo_now,
                                        s->todo_now[0] ? COL_ACCENT : COL_DIM, 0);
        } else {
            lv_label_set_text(detail_todo_lbl, "No to-dos");
            lv_bar_set_value(detail_todo_bar, 0, LV_ANIM_OFF);
            lv_obj_add_flag(detail_todo_bar, LV_OBJ_FLAG_HIDDEN);
            lv_label_set_text(detail_todo_now, "");
        }
    } else {
        lv_obj_add_flag(detail_act_panel, LV_OBJ_FLAG_HIDDEN);
        lv_obj_add_flag(detail_todo_panel, LV_OBJ_FLAG_HIDDEN);
        lv_obj_clear_flag(detail_unavail, LV_OBJ_FLAG_HIDDEN);
    }
}

// ======== Public API ========

void ui_init(void) {
    compute_layout(board_caps());

    lv_obj_t* scr = lv_screen_active();
    lv_obj_set_style_bg_color(scr, COL_BG, 0);
    lv_obj_set_style_bg_opa(scr, LV_OPA_COVER, 0);

    init_icon_dsc_rgb565a8(&logo_dsc, LOGO_WIDTH, LOGO_HEIGHT, logo_data);
    init_battery_icons();

    init_usage_screen(scr);
    build_activity_screen(scr);
    build_session_detail_screen(scr);
    splash_init(scr);

    if (splash_get_root()) {
        lv_obj_add_event_cb(splash_get_root(), global_click_cb, LV_EVENT_CLICKED, NULL);
    }

    logo_img = lv_image_create(scr);
    lv_image_set_src(logo_img, &logo_dsc);
    lv_obj_set_pos(logo_img, L.margin, L.title_y - 10);

    battery_img = lv_image_create(scr);
    lv_image_set_src(battery_img, &battery_dscs[0]);
    lv_obj_set_pos(battery_img, L.scr_w - 48 - L.margin, L.title_y);

}

void ui_update(const UsageData* data) {
    if (!data->valid) return;
    last_data_ms = lv_tick_get();   // a valid usage update just landed → dot goes green
    data_received = true;

    int s_pct = (int)(data->session_pct + 0.5f);

    lv_label_set_text_fmt(lbl_session_pct, "%d%%", s_pct);
    lv_bar_set_value(bar_session, s_pct, LV_ANIM_ON);
    lv_obj_set_style_bg_color(bar_session, pct_color(data->session_pct), LV_PART_INDICATOR);

    char buf[48];
    format_reset_time(data->session_reset_mins, buf, sizeof(buf));
    lv_label_set_text(lbl_session_reset, buf);

    int w_pct = (int)(data->weekly_pct + 0.5f);
    lv_label_set_text_fmt(lbl_weekly_pct, "%d%%", w_pct);
    lv_bar_set_value(bar_weekly, w_pct, LV_ANIM_ON);
    lv_obj_set_style_bg_color(bar_weekly, pct_color(data->weekly_pct), LV_PART_INDICATOR);

    format_reset_time(data->weekly_reset_mins, buf, sizeof(buf));
    lv_label_set_text(lbl_weekly_reset, buf);
}

void ui_update_activity(const ActivityData* data) {
    if (!activity_list) return;
    if (data) s_activity_cache = *data;   // keep latest for the detail screen
    int n = (data && data->valid) ? data->count : 0;
    if (n > MAX_SESSIONS) n = MAX_SESSIONS;
    for (int i = 0; i < MAX_SESSIONS; i++) {
        if (i < n) {
            const SessionData& s = data->sessions[i];
            lv_label_set_text(row_proj[i], s.project[0] ? s.project : "(session)");
            // model, big, top-right (e.g. "Opus 4.8")
            char mbuf[20];
            format_model(s.model, mbuf, sizeof(mbuf));
            lv_label_set_text(row_modelbig[i], mbuf);
            // running/idle status, bottom-left
            if (s.working) {
                lv_label_set_text(row_status[i], "Running");
                lv_obj_set_style_text_color(row_status[i], COL_ACCENT, 0);
            } else {
                char t[16], sbuf[24];
                fmt_idle(s.idle_secs, t, sizeof(t));
                snprintf(sbuf, sizeof(sbuf), "idle %s", t);
                lv_label_set_text(row_status[i], sbuf);
                lv_obj_set_style_text_color(row_status[i], COL_DIM, 0);
            }
            // effort, bottom, left of the %
            lv_label_set_text(row_effort[i], s.effort);
            int p = s.ctx_pct; if (p < 0) p = 0; if (p > 100) p = 100;
            lv_bar_set_value(row_bar[i], p, LV_ANIM_OFF);
            lv_obj_set_style_bg_color(row_bar[i], pct_color((float)p), LV_PART_INDICATOR);
            lv_label_set_text_fmt(row_pct[i], "%d%%", p);
            lv_obj_clear_flag(row_panel[i], LV_OBJ_FLAG_HIDDEN);
        } else {
            lv_obj_add_flag(row_panel[i], LV_OBJ_FLAG_HIDDEN);
        }
    }
    if (n == 0) lv_obj_clear_flag(activity_empty, LV_OBJ_FLAG_HIDDEN);
    else        lv_obj_add_flag(activity_empty, LV_OBJ_FLAG_HIDDEN);

    // Keep an open detail screen live; drop back to the list if its session
    // has disappeared (count shrank / pruned).
    if (current_screen == SCREEN_SESSION_DETAIL) {
        if (selected_session >= 0 && selected_session < n)
            ui_update_session_detail(&s_activity_cache.sessions[selected_session]);
        else
            ui_show_screen(SCREEN_ACTIVITY);
    }
}

// Pick the usage-view sub-screen: pairing hint (BLE down), the idle "Zzz" screen
// (connected but data has gone stale), or the live usage panels. Only re-lays-out
// on an actual change. The animated status line stays visible everywhere — it
// reads "Listening…" on the idle screen, keeping it alive rather than frozen.
static void update_view_state(void) {
    if (!usage_group || !pair_group || !idle_group) return;
    int v;
    if (!s_ble_connected) {
        v = 0;  // pairing hint
    } else if (data_received && (lv_tick_get() - last_data_ms) < DATA_FRESH_MS) {
        v = 2;  // live usage
    } else {
        v = 1;  // idle / Zzz
    }
    if (v == view_state) return;
    view_state = v;
    lv_obj_add_flag(pair_group, LV_OBJ_FLAG_HIDDEN);
    lv_obj_add_flag(idle_group, LV_OBJ_FLAG_HIDDEN);
    lv_obj_add_flag(usage_group, LV_OBJ_FLAG_HIDDEN);
    lv_obj_clear_flag(v == 0 ? pair_group : v == 1 ? idle_group : usage_group,
                      LV_OBJ_FLAG_HIDDEN);
}

void ui_tick_anim(void) {
    if (current_screen != SCREEN_USAGE) return;
    update_view_state();
    if (view_state == 1) splash_mini_tick(&s_idle_mini);   // animate the sleeping creature on the idle screen
    if (s_working)       splash_mini_tick(&s_work_mini);   // animate the top-left "working" badge

    uint32_t now = lv_tick_get();

    if (now - anim_msg_start >= ANIM_MSG_MS) {
        anim_msg_idx = (anim_msg_idx + 1) % ANIM_MSG_COUNT;
        anim_msg_start = now;
    }

    if (now - anim_last_ms < spinner_ms[anim_spinner_idx]) return;
    anim_last_ms = now;
    anim_phase = (anim_phase + 1) % SPINNER_PHASES;
    anim_spinner_idx = (anim_phase < SPINNER_COUNT) ? anim_phase
                                                    : (SPINNER_PHASES - anim_phase);

    // Status text by priority. Whimsical messages only when connected & settled.
    const char* text;
    if (!s_ble_connected) {
        text = "Waiting";              // advertising / waiting for a host connection
    } else if (view_state == 1) {      // idle — alternate so it reads as alive AND data-less
        text = (anim_msg_idx & 1) ? "No data" : "Listening";
    } else if (now - connected_at_ms < 5000) {
        text = "Connected";
    } else {
        text = anim_messages[anim_msg_idx];
    }

    // All states share the whimsical style: "<glyph> <Title-case word>…"
    static char buf[80];
    snprintf(buf, sizeof(buf), "%s %s\xE2\x80\xA6",
             spinner_frames[anim_spinner_idx], text);
    lv_label_set_text(lbl_anim, buf);
}

static screen_t prev_non_splash_screen = SCREEN_USAGE;
static void apply_battery_visibility(void) {
    if (!battery_img) return;
    if (current_screen == SCREEN_SPLASH) lv_obj_add_flag(battery_img, LV_OBJ_FLAG_HIDDEN);
    else                                  lv_obj_clear_flag(battery_img, LV_OBJ_FLAG_HIDDEN);
}

// Logo shows on non-splash screens, EXCEPT while working (the work-coding badge
// takes its top-left spot). The badge lives inside usage_container, so it's
// naturally hidden whenever the splash screen is up.
static void apply_logo_visibility(void) {
    if (!logo_img) return;
    bool show = (current_screen != SCREEN_SPLASH) && !s_working;
    if (show) lv_obj_clear_flag(logo_img, LV_OBJ_FLAG_HIDDEN);
    else      lv_obj_add_flag(logo_img, LV_OBJ_FLAG_HIDDEN);
}

static void apply_work_mini_visibility(void) {
    if (!work_mini_canvas) return;
    if (s_working) lv_obj_clear_flag(work_mini_canvas, LV_OBJ_FLAG_HIDDEN);
    else           lv_obj_add_flag(work_mini_canvas, LV_OBJ_FLAG_HIDDEN);
}

static void global_click_cb(lv_event_t* e) {
    (void)e;
    // Tap (on the heading / empty area) cycles SPLASH -> USAGE -> ACTIVITY ->
    // SPLASH. Session detail is reached via row_click_cb and left via
    // detail_back_cb — it is NOT part of this ring.
    switch (current_screen) {
        case SCREEN_SPLASH:   ui_show_screen(SCREEN_USAGE);    break;
        case SCREEN_USAGE:    ui_show_screen(SCREEN_ACTIVITY); break;
        case SCREEN_ACTIVITY: ui_show_screen(SCREEN_SPLASH);   break;
        default:              ui_show_screen(SCREEN_SPLASH);   break;
    }
}

// Tap on an Activity row → open that session's detail screen.
static void row_click_cb(lv_event_t* e) {
    int idx = (int)(intptr_t)lv_event_get_user_data(e);
    if (idx < 0 || idx >= s_activity_cache.count) return;
    selected_session = idx;
    ui_update_session_detail(&s_activity_cache.sessions[idx]);
    ui_show_screen(SCREEN_SESSION_DETAIL);
}

// Tap anywhere on the detail screen → back to the Activity list.
static void detail_back_cb(lv_event_t* e) {
    (void)e;
    ui_show_screen(SCREEN_ACTIVITY);
}

void ui_show_screen(screen_t screen) {
    lv_obj_add_flag(usage_container, LV_OBJ_FLAG_HIDDEN);
    if (activity_container) lv_obj_add_flag(activity_container, LV_OBJ_FLAG_HIDDEN);
    if (detail_container)   lv_obj_add_flag(detail_container, LV_OBJ_FLAG_HIDDEN);
    splash_hide();

    switch (screen) {
    case SCREEN_SPLASH:   splash_show(); break;
    case SCREEN_USAGE:    lv_obj_clear_flag(usage_container, LV_OBJ_FLAG_HIDDEN); break;
    case SCREEN_ACTIVITY: if (activity_container) lv_obj_clear_flag(activity_container, LV_OBJ_FLAG_HIDDEN); break;
    case SCREEN_SESSION_DETAIL: if (detail_container) lv_obj_clear_flag(detail_container, LV_OBJ_FLAG_HIDDEN); break;
    default: break;
    }

    // Record the last "main" screen for the PWR/idle splash toggle. Detail is a
    // transient drill-in, so toggling splash should return to the list, not detail.
    if (screen == SCREEN_USAGE || screen == SCREEN_ACTIVITY) prev_non_splash_screen = screen;
    current_screen = screen;
    apply_logo_visibility();
    apply_work_mini_visibility();
    apply_battery_visibility();
}

void ui_toggle_splash(void) {
    if (current_screen == SCREEN_SPLASH) ui_show_screen(prev_non_splash_screen);
    else                                  ui_show_screen(SCREEN_SPLASH);
}

void ui_set_working(bool working) {
    if (working == s_working) return;
    s_working = working;
    apply_logo_visibility();
    apply_work_mini_visibility();
}

screen_t ui_get_current_screen(void) {
    return current_screen;
}

void ui_update_ble_status(ble_state_t state, const char* name, const char* mac) {
    (void)name; (void)mac;
    bool was_connected = s_ble_connected;
    s_ble_connected = (state == BLE_STATE_CONNECTED);

    if (s_ble_connected && !was_connected) connected_at_ms = lv_tick_get();
    // pair / idle / usage — picked from connection + data freshness.
    update_view_state();
}

void ui_update_battery(int percent, bool charging) {
    int idx;
    if (charging) {
        idx = 4;
    } else if (percent < 0) {
        idx = 0;
    } else if (percent <= 10) {
        idx = 0;
    } else if (percent <= 35) {
        idx = 1;
    } else if (percent <= 75) {
        idx = 2;
    } else {
        idx = 3;
    }
    lv_image_set_src(battery_img, &battery_dscs[idx]);
    apply_battery_visibility();
}
