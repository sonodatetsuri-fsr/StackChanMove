#include "wifi_manager.h"

static const char* s_ssid = nullptr;
static const char* s_password = nullptr;
static const unsigned long CONNECT_TIMEOUT_MS = 30000;
static const unsigned long RECONNECT_INTERVAL_MS = 5000;
static unsigned long s_last_reconnect_attempt = 0;

bool wifi_init(const char* ssid, const char* password) {
    s_ssid = ssid;
    s_password = password;

    Serial.printf("[WiFi] Connecting to %s ...\n", s_ssid);
    WiFi.mode(WIFI_STA);
    // NOTE: WiFi.setSleep(false) was tried against ws_poll() receive-latency
    // bursts and made playback measurably WORSE in the field —
    // do not re-add it. The fix for ws stalls is decoupling the speaker feed
    // from loop() instead.
    WiFi.begin(s_ssid, s_password);

    unsigned long start = millis();
    while (WiFi.status() != WL_CONNECTED) {
        if (millis() - start >= CONNECT_TIMEOUT_MS) {
            Serial.println("[WiFi] Connection timed out");
            WiFi.disconnect();  // stop the in-progress connect; a lingering attempt blocks
                                // setup-GUI scans
            return false;
        }
        delay(500);
        Serial.print(".");
    }

    Serial.println();
    Serial.printf("[WiFi] Connected — IP: %s\n", WiFi.localIP().toString().c_str());
    return true;
}

void wifi_ensure_connected() {
    if (WiFi.status() == WL_CONNECTED) {
        return;
    }

    unsigned long now = millis();
    if (now - s_last_reconnect_attempt < RECONNECT_INTERVAL_MS) {
        return;
    }
    s_last_reconnect_attempt = now;

    Serial.println("[WiFi] Disconnected — attempting reconnect...");
    WiFi.disconnect();
    WiFi.begin(s_ssid, s_password);
}
