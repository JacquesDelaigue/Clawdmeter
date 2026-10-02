#pragma once
#include <stdint.h>
#include <lvgl.h>

// Initialize splash module. Creates the canvas widget inside `parent` and
// allocates the 480x480 pixel buffer (PSRAM).
void splash_init(lv_obj_t *parent);

// Advance animation frame if hold time elapsed. Call from main loop.
void splash_tick(void);

// Cycle to the next animation in the catalog.
void splash_next(void);

// Show/hide the splash container.
void splash_show(void);
void splash_hide(void);

// Pick the next animation: the busy animation while working (see below),
// otherwise the next one in the idle rotation (usage-rate groups for the
// official set). Called automatically by splash_show().
void splash_pick_for_current_rate(void);

// The usage-rate group changed while the splash is showing. Re-picks only when
// the idle rotation is rate-driven (official set); a no-op for Jacques's flat
// playlist, which is deliberately independent of usage rate.
void splash_on_rate_group_change(void);

// True if the compiled-in catalog has an animation with this exact name.
bool splash_has_anim(const char *name);

// Working-mode (fork feature): while Claude Code is actively running the splash
// "homes" to the busy animation ("work coding" when Jacques's set is compiled
// in, else the official "laptop"). The PWR button can still cycle animations,
// but the splash snaps back a few seconds after the last manual cycle (while
// still working). Driven by the daemon's "working" flag.
void splash_set_working(bool working);
void splash_note_manual(void);   // call when the user manually cycles (PWR button)
bool splash_get_working(void);

// True when splash is currently rendering (used to gate re-picks).
bool splash_is_active(void);

// Root container (so ui.cpp can attach a click event).
lv_obj_t* splash_get_root(void);

// Mini animated creature for embedding elsewhere (e.g. the idle screen, or a
// small "working" badge on the usage meter). Each instance is caller-owned via
// a splash_mini_t, so several can run at once (fork feature). Renders the named
// animation so its longer side is ~px (integer px per cell) inside `parent`;
// returns the canvas object (position it with lv_obj_set_pos/lv_obj_align) or
// NULL if the animation isn't found / allocation fails. Drive it with
// splash_mini_tick(&handle).
typedef struct {
    int       anim_idx;   // index into the animation catalog; -1 = none
    uint16_t* buf;
    lv_obj_t* canvas;
    uint16_t  frame;
    uint32_t  started;
    int       cell;
    int       w, h;       // canvas px
} splash_mini_t;

lv_obj_t* splash_mini_create(splash_mini_t *m, lv_obj_t *parent, const char *anim_name, int px);
void splash_mini_tick(splash_mini_t *m);

// Corner mascot (usage screen, PSRAM boards): the still Clawd idles in the
// logo slot, does occasional acts, and takes walk-off/lurk/walk-back trips.
// feet_y = px of the art's ground line; cell = px per art cell in the corner.
lv_obj_t* splash_mascot_create(lv_obj_t *parent, int slot_x, int feet_y, int cell);
void splash_mascot_tick(void);
void splash_mascot_set_visible(bool v);
