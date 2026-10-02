// Native entry point — stands in for the Arduino runtime. loop()'s own
// delay(5) paces the loop the same way it does on hardware.
#include <Arduino.h>
#include <stdlib.h>
#include <string.h>
#include "sim_platform.h"
#include "../../ui.h"
#include "../../splash.h"

// Optional start state for headless screenshots (fork addition):
//   SIM_START_SCREEN=usage   start on the usage screen instead of the splash
//   SIM_SPLASH_ANIM=<name>   start the splash on this animation (e.g. "skate")
static void apply_start_env(void) {
    const char* scr = getenv("SIM_START_SCREEN");
    if (scr && strcmp(scr, "usage") == 0) ui_show_screen(SCREEN_USAGE);
    const char* anim = getenv("SIM_SPLASH_ANIM");
    if (anim && !splash_select(anim)) printf("SIM_SPLASH_ANIM: no animation named \"%s\"\n", anim);
}

int main(void) {
    printf(
        "Clawdmeter simulator\n"
        "  mouse          touch (tap toggles splash/usage)\n"
        "  space          play/pause scenario    left/right step    1-9 jump\n"
        "  d              toggle BLE link\n"
        "  b / n (hold)   BOOT / secondary button    p  PWR button\n"
        "  c              toggle charging            - / =  battery down/up\n"
        "  s              screenshot                 esc  quit\n\n");
    setup();
    apply_start_env();
    while (!sim_should_quit()) {
        sim_pump();
        loop();
    }
    return 0;
}
