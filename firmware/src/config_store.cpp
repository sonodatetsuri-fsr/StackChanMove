#include "config_store.h"

#include <Preferences.h>

static Preferences prefs;

void config_store_init() {
    prefs.begin("stackchan", false);
}

bool config_store_has_config() {
    return config_store_get_ssid().length() > 0 && config_store_get_hub_url().length() > 0;
}

String config_store_get_ssid() {
    return prefs.getString("wifi_ssid", "");
}

String config_store_get_password() {
    return prefs.getString("wifi_pass", "");
}

String config_store_get_hub_url() {
    return prefs.getString("hub_url", "");
}

void config_store_save(const String& ssid, const String& password, const String& hub_url) {
    prefs.putString("wifi_ssid", ssid);
    prefs.putString("wifi_pass", password);
    prefs.putString("hub_url", hub_url);
    Serial.printf("[ConfigStore] Saved ssid=%s hub_url=%s\n", ssid.c_str(), hub_url.c_str());
}

void config_store_clear() {
    prefs.clear();
    Serial.println("[ConfigStore] Cleared");
}
