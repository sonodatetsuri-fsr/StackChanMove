#pragma once
#include <Arduino.h>

struct SetupResult {
    String ssid;
    String password;
    String hub_url;
    bool completed;  // false = cancelled (return to existing config)
};

SetupResult setup_gui_run();
