#include "setup_gui.h"

#include <M5Unified.h>
#include <WiFi.h>

#include <algorithm>
#include <cctype>
#include <cstring>
#include <vector>

#include "config.h"
#include "config_store.h"

// --- Layout constants -------------------------------------------------
static const int SSID_ROW_H = 28;
static const int SSID_START_Y = 30;
static const int INPUT_Y = 10;
static const int INPUT_H = 40;
static const int KB_Y = 100;
static const int KEY_W = 28;
static const int KEY_H = 30;
static const int KEY_GAP = 2;

// =======================================================================
// Screen 1: WiFi selection
// =======================================================================

struct WifiEntry {
    String ssid;
    int32_t rssi;
};

struct WifiSelectResult {
    String ssid;
    bool cancelled;
};

static const int SSIDS_PER_PAGE = 5;

static int rssi_to_bars(int32_t rssi) {
    if (rssi >= -55) return 4;
    if (rssi >= -65) return 3;
    if (rssi >= -75) return 2;
    return 1;
}

static void draw_signal_bars(int x, int y, int bars) {
    for (int i = 0; i < 4; i++) {
        int bar_h = 4 + i * 3;
        int bx = x + i * 6;
        int by = y + (12 - bar_h);
        uint16_t color = (i < bars) ? TFT_GREEN : TFT_DARKGREY;
        M5.Display.fillRect(bx, by, 4, bar_h, color);
    }
}

static int scan_wifi(std::vector<WifiEntry>& networks) {
    Serial.println("[SetupGUI] Scanning WiFi networks...");
    WiFi.mode(WIFI_STA);
    WiFi.disconnect();  // scan start is rejected (WIFI_SCAN_FAILED) while STA is connecting
    delay(100);
    int n = WiFi.scanNetworks();
    networks.clear();
    for (int i = 0; i < n; i++) {
        WifiEntry e;
        e.ssid = WiFi.SSID(i);
        e.rssi = WiFi.RSSI(i);
        networks.push_back(e);
    }
    std::sort(networks.begin(), networks.end(),
              [](const WifiEntry& a, const WifiEntry& b) { return a.rssi > b.rssi; });
    if (networks.size() > 20) {
        networks.resize(20);
    }
    Serial.printf("[SetupGUI] Found %d networks (showing %d)\n", n, (int)networks.size());
    return n;
}

static void draw_wifi_select_screen(const std::vector<WifiEntry>& networks, bool has_existing,
                                    int page, int scan_result) {
    int w = M5.Display.width();
    int h = M5.Display.height();
    int total = (int)networks.size();
    int total_pages = (total + SSIDS_PER_PAGE - 1) / SSIDS_PER_PAGE;
    if (total_pages < 1) total_pages = 1;
    bool has_nav = total_pages > 1;

    int start_idx = page * SSIDS_PER_PAGE;
    int end_idx = start_idx + SSIDS_PER_PAGE;
    if (end_idx > total) end_idx = total;
    int visible = end_idx - start_idx;

    int action_y = h - SSID_ROW_H - 2;
    int nav_y = has_nav ? action_y - SSID_ROW_H - 4 : -1;

    M5.Display.fillScreen(TFT_BLACK);

    M5.Display.setTextDatum(textdatum_t::top_left);
    M5.Display.setTextSize(1);
    M5.Display.setTextColor(TFT_WHITE);
    M5.Display.drawString("Select WiFi Network", 4, 4);

    for (int i = 0; i < visible; i++) {
        int idx = start_idx + i;
        int y = SSID_START_Y + i * SSID_ROW_H;
        M5.Display.drawRect(4, y, w - 8, SSID_ROW_H - 2, TFT_LIGHTGREY);
        M5.Display.setTextDatum(textdatum_t::middle_left);
        M5.Display.setTextColor(TFT_WHITE);
        M5.Display.drawString(networks[idx].ssid, 10, y + (SSID_ROW_H - 2) / 2);
        draw_signal_bars(w - 40, y + 6, rssi_to_bars(networks[idx].rssi));
    }

    if (total == 0) {
        M5.Display.setTextDatum(textdatum_t::middle_center);
        M5.Display.setTextColor(TFT_LIGHTGREY);
        const char* msg = (scan_result < 0) ? "Scan failed - tap Rescan" : "No networks found";
        M5.Display.drawString(msg, w / 2, h / 2);
    }

    if (has_nav) {
        int third_w = (w - 16) / 3;
        if (page > 0) {
            M5.Display.fillRect(4, nav_y, third_w, SSID_ROW_H, TFT_DARKGREY);
            M5.Display.drawRect(4, nav_y, third_w, SSID_ROW_H, TFT_WHITE);
            M5.Display.setTextDatum(textdatum_t::middle_center);
            M5.Display.setTextColor(TFT_WHITE);
            M5.Display.drawString("Prev", 4 + third_w / 2, nav_y + SSID_ROW_H / 2);
        }
        char page_str[16];
        snprintf(page_str, sizeof(page_str), "%d/%d", page + 1, total_pages);
        M5.Display.setTextDatum(textdatum_t::middle_center);
        M5.Display.setTextColor(TFT_LIGHTGREY);
        M5.Display.drawString(page_str, w / 2, nav_y + SSID_ROW_H / 2);
        if (page < total_pages - 1) {
            M5.Display.fillRect(w - 4 - third_w, nav_y, third_w, SSID_ROW_H, TFT_DARKGREY);
            M5.Display.drawRect(w - 4 - third_w, nav_y, third_w, SSID_ROW_H, TFT_WHITE);
            M5.Display.setTextDatum(textdatum_t::middle_center);
            M5.Display.setTextColor(TFT_WHITE);
            M5.Display.drawString("Next", w - 4 - third_w / 2, nav_y + SSID_ROW_H / 2);
        }
    }

    if (has_existing) {
        int btn_w = (w - 8 - 8) / 2;
        M5.Display.fillRect(4, action_y, btn_w, SSID_ROW_H, TFT_BLUE);
        M5.Display.drawRect(4, action_y, btn_w, SSID_ROW_H, TFT_WHITE);
        M5.Display.setTextDatum(textdatum_t::middle_center);
        M5.Display.setTextColor(TFT_WHITE);
        M5.Display.drawString("Rescan", 4 + btn_w / 2, action_y + SSID_ROW_H / 2);
        M5.Display.fillRect(4 + btn_w + 8, action_y, btn_w, SSID_ROW_H, TFT_DARKGREY);
        M5.Display.drawRect(4 + btn_w + 8, action_y, btn_w, SSID_ROW_H, TFT_WHITE);
        M5.Display.drawString("Back", 4 + btn_w + 8 + btn_w / 2, action_y + SSID_ROW_H / 2);
    } else {
        M5.Display.fillRect(4, action_y, w - 8, SSID_ROW_H, TFT_BLUE);
        M5.Display.drawRect(4, action_y, w - 8, SSID_ROW_H, TFT_WHITE);
        M5.Display.setTextDatum(textdatum_t::middle_center);
        M5.Display.setTextColor(TFT_WHITE);
        M5.Display.drawString("Rescan", w / 2, action_y + SSID_ROW_H / 2);
    }
}

static WifiSelectResult run_wifi_select_screen(bool has_existing) {
    std::vector<WifiEntry> networks;
    int scan_result = scan_wifi(networks);
    int page = 0;
    int w = M5.Display.width();
    int h = M5.Display.height();

    draw_wifi_select_screen(networks, has_existing, page, scan_result);

    while (true) {
        M5.update();
        auto t = M5.Touch.getDetail();
        if (!t.wasPressed()) {
            delay(20);
            continue;
        }
        int x = t.x;
        int y = t.y;

        int total = (int)networks.size();
        int total_pages = (total + SSIDS_PER_PAGE - 1) / SSIDS_PER_PAGE;
        if (total_pages < 1) total_pages = 1;
        bool has_nav = total_pages > 1;
        int action_y = h - SSID_ROW_H - 2;
        int nav_y = has_nav ? action_y - SSID_ROW_H - 4 : -1;

        int start_idx = page * SSIDS_PER_PAGE;
        int end_idx = start_idx + SSIDS_PER_PAGE;
        if (end_idx > total) end_idx = total;
        int visible = end_idx - start_idx;

        for (int i = 0; i < visible; i++) {
            int ry = SSID_START_Y + i * SSID_ROW_H;
            if (x >= 4 && x <= w - 4 && y >= ry && y <= ry + SSID_ROW_H - 2) {
                int idx = start_idx + i;
                Serial.printf("[SetupGUI] Selected SSID: %s\n", networks[idx].ssid.c_str());
                WifiSelectResult r;
                r.ssid = networks[idx].ssid;
                r.cancelled = false;
                return r;
            }
        }

        if (has_nav) {
            int third_w = (w - 16) / 3;
            if (page > 0 && x >= 4 && x <= 4 + third_w && y >= nav_y && y <= nav_y + SSID_ROW_H) {
                page--;
                draw_wifi_select_screen(networks, has_existing, page, scan_result);
                continue;
            }
            if (page < total_pages - 1 && x >= w - 4 - third_w && x <= w - 4 && y >= nav_y &&
                y <= nav_y + SSID_ROW_H) {
                page++;
                draw_wifi_select_screen(networks, has_existing, page, scan_result);
                continue;
            }
        }

        if (has_existing) {
            int btn_w = (w - 8 - 8) / 2;
            if (x >= 4 && x <= 4 + btn_w && y >= action_y && y <= action_y + SSID_ROW_H) {
                scan_result = scan_wifi(networks);
                page = 0;
                draw_wifi_select_screen(networks, has_existing, page, scan_result);
                continue;
            }
            if (x >= 4 + btn_w + 8 && x <= 4 + btn_w + 8 + btn_w && y >= action_y &&
                y <= action_y + SSID_ROW_H) {
                Serial.println("[SetupGUI] Back tapped - cancelling setup");
                WifiSelectResult r;
                r.cancelled = true;
                return r;
            }
        } else {
            if (x >= 4 && x <= w - 4 && y >= action_y && y <= action_y + SSID_ROW_H) {
                scan_result = scan_wifi(networks);
                page = 0;
                draw_wifi_select_screen(networks, has_existing, page, scan_result);
                continue;
            }
        }
        delay(20);
    }
}

// =======================================================================
// Screens 2/3: software keyboard input
// =======================================================================

enum KeyAction { KEY_CHAR, KEY_SHIFT, KEY_MODE, KEY_BACKSPACE, KEY_SPACE, KEY_OK };

struct KbKey {
    int x, y, w, h;
    String label;
    KeyAction action;
    char ch;
};

static void build_keyboard_layout(bool shift, bool numeric, std::vector<KbKey>& keys) {
    keys.clear();
    int w = M5.Display.width();
    const int SIDE_KEY_W = 40;

    // Row 0
    const char* row0 = numeric ? "1234567890" : "qwertyuiop";
    int n0 = (int)strlen(row0);
    int total_w0 = n0 * KEY_W + (n0 - 1) * KEY_GAP;
    int start_x0 = (w - total_w0) / 2;
    int y0 = KB_Y;
    for (int i = 0; i < n0; i++) {
        KbKey k;
        k.x = start_x0 + i * (KEY_W + KEY_GAP);
        k.y = y0;
        k.w = KEY_W;
        k.h = KEY_H;
        char c = row0[i];
        if (!numeric && shift) c = (char)toupper(c);
        k.ch = c;
        k.label = String(c);
        k.action = KEY_CHAR;
        keys.push_back(k);
    }

    // Row 1
    const char* row1 = numeric ? "-/:;()$&@\"" : "asdfghjkl";
    int n1 = (int)strlen(row1);
    int total_w1 = n1 * KEY_W + (n1 - 1) * KEY_GAP;
    int start_x1 = (w - total_w1) / 2;
    int y1 = y0 + KEY_H + KEY_GAP;
    for (int i = 0; i < n1; i++) {
        KbKey k;
        k.x = start_x1 + i * (KEY_W + KEY_GAP);
        k.y = y1;
        k.w = KEY_W;
        k.h = KEY_H;
        char c = row1[i];
        if (!numeric && shift) c = (char)toupper(c);
        k.ch = c;
        k.label = String(c);
        k.action = KEY_CHAR;
        keys.push_back(k);
    }

    // Row 2
    int y2 = y1 + KEY_H + KEY_GAP;
    if (!numeric) {
        const char* row2 = "zxcvbnm";
        int n2 = (int)strlen(row2);
        int total_w2 = n2 * KEY_W + (n2 - 1) * KEY_GAP;
        int start_x2 = (w - total_w2) / 2;

        KbKey shift_key;
        shift_key.x = start_x2 - KEY_GAP - SIDE_KEY_W;
        shift_key.y = y2;
        shift_key.w = SIDE_KEY_W;
        shift_key.h = KEY_H;
        shift_key.label = "Shift";
        shift_key.action = KEY_SHIFT;
        shift_key.ch = 0;
        keys.push_back(shift_key);

        for (int i = 0; i < n2; i++) {
            KbKey k;
            k.x = start_x2 + i * (KEY_W + KEY_GAP);
            k.y = y2;
            k.w = KEY_W;
            k.h = KEY_H;
            char c = row2[i];
            if (shift) c = (char)toupper(c);
            k.ch = c;
            k.label = String(c);
            k.action = KEY_CHAR;
            keys.push_back(k);
        }

        KbKey bs;
        bs.x = start_x2 + n2 * (KEY_W + KEY_GAP);
        bs.y = y2;
        bs.w = SIDE_KEY_W;
        bs.h = KEY_H;
        bs.label = "Bksp";
        bs.action = KEY_BACKSPACE;
        bs.ch = 0;
        keys.push_back(bs);
    } else {
        const char* row2 = ".,?!'";
        int n2 = (int)strlen(row2);
        int total_w2 = n2 * KEY_W + (n2 - 1) * KEY_GAP;
        int start_x2 = (w - total_w2) / 2;

        for (int i = 0; i < n2; i++) {
            KbKey k;
            k.x = start_x2 + i * (KEY_W + KEY_GAP);
            k.y = y2;
            k.w = KEY_W;
            k.h = KEY_H;
            k.ch = row2[i];
            k.label = String(row2[i]);
            k.action = KEY_CHAR;
            keys.push_back(k);
        }

        KbKey bs;
        bs.x = w - 4 - SIDE_KEY_W;
        bs.y = y2;
        bs.w = SIDE_KEY_W;
        bs.h = KEY_H;
        bs.label = "Bksp";
        bs.action = KEY_BACKSPACE;
        bs.ch = 0;
        keys.push_back(bs);
    }

    // Bottom row: mode toggle, space, OK
    int y3 = y2 + KEY_H + KEY_GAP;

    KbKey mode_key;
    mode_key.x = 4;
    mode_key.y = y3;
    mode_key.w = 50;
    mode_key.h = KEY_H;
    mode_key.label = numeric ? "ABC" : "123";
    mode_key.action = KEY_MODE;
    mode_key.ch = 0;
    keys.push_back(mode_key);

    KbKey ok_key;
    ok_key.w = 50;
    ok_key.h = KEY_H;
    ok_key.x = w - 4 - ok_key.w;
    ok_key.y = y3;
    ok_key.label = "OK";
    ok_key.action = KEY_OK;
    ok_key.ch = 0;
    keys.push_back(ok_key);

    KbKey space_key;
    space_key.x = mode_key.x + mode_key.w + KEY_GAP;
    space_key.y = y3;
    space_key.w = ok_key.x - KEY_GAP - space_key.x;
    space_key.h = KEY_H;
    space_key.label = "Space";
    space_key.action = KEY_SPACE;
    space_key.ch = 0;
    keys.push_back(space_key);
}

static String keyboard_input(const char* title, bool masked, const String& initial) {
    String value = initial;
    bool shift = false;
    bool numeric = false;
    bool reveal = false;
    int w = M5.Display.width();
    int h = M5.Display.height();

    int reveal_btn_w = 56;
    int reveal_btn_h = INPUT_H;
    int reveal_btn_x = w - reveal_btn_w;
    int reveal_btn_y = INPUT_Y;
    int field_w = masked ? (w - reveal_btn_w - 4) : w;

    std::vector<KbKey> keys;

    auto redraw_input = [&]() {
        M5.Display.fillRect(0, INPUT_Y, field_w, INPUT_H, TFT_BLACK);
        M5.Display.drawRect(0, INPUT_Y, field_w, INPUT_H, TFT_WHITE);
        String display_text;
        if (masked && !reveal) {
            for (size_t i = 0; i < value.length(); i++) display_text += '*';
        } else {
            display_text = value;
        }
        M5.Display.setTextSize(2);
        int visible_w = field_w - 12;
        while (M5.Display.textWidth(display_text) > visible_w && display_text.length() > 0) {
            display_text = display_text.substring(1);
        }
        M5.Display.setTextDatum(textdatum_t::middle_left);
        M5.Display.setTextColor(TFT_WHITE);
        M5.Display.drawString(display_text, 6, INPUT_Y + INPUT_H / 2);
    };

    auto draw_reveal_button = [&]() {
        if (!masked) return;
        M5.Display.fillRect(reveal_btn_x, reveal_btn_y, reveal_btn_w, reveal_btn_h, TFT_DARKGREY);
        M5.Display.drawRect(reveal_btn_x, reveal_btn_y, reveal_btn_w, reveal_btn_h, TFT_WHITE);
        M5.Display.setTextDatum(textdatum_t::middle_center);
        M5.Display.setTextSize(1);
        M5.Display.setTextColor(TFT_WHITE);
        M5.Display.drawString(reveal ? "Hide" : "Show", reveal_btn_x + reveal_btn_w / 2,
                              reveal_btn_y + reveal_btn_h / 2);
    };

    auto draw_title = [&]() {
        M5.Display.setTextDatum(textdatum_t::top_left);
        M5.Display.setTextSize(1);
        M5.Display.setTextColor(TFT_LIGHTGREY);
        M5.Display.drawString(title, 4, 1);
    };

    auto draw_keyboard = [&]() {
        build_keyboard_layout(shift, numeric, keys);
        M5.Display.fillRect(0, KB_Y - 4, w, h - (KB_Y - 4), TFT_BLACK);
        for (auto& k : keys) {
            uint16_t fill = TFT_DARKGREY;
            if (k.action == KEY_SHIFT && shift) fill = TFT_GREEN;
            if (k.action == KEY_OK) fill = TFT_BLUE;
            M5.Display.fillRect(k.x, k.y, k.w, k.h, fill);
            M5.Display.drawRect(k.x, k.y, k.w, k.h, TFT_WHITE);
            M5.Display.setTextDatum(textdatum_t::middle_center);
            M5.Display.setTextSize(1);
            M5.Display.setTextColor(TFT_WHITE);
            M5.Display.drawString(k.label, k.x + k.w / 2, k.y + k.h / 2);
        }
    };

    auto full_redraw = [&]() {
        M5.Display.fillScreen(TFT_BLACK);
        draw_title();
        redraw_input();
        draw_reveal_button();
        draw_keyboard();
    };

    full_redraw();

    while (true) {
        M5.update();
        auto t = M5.Touch.getDetail();
        if (t.wasPressed()) {
            int x = t.x;
            int y = t.y;

            if (masked && x >= reveal_btn_x && x <= reveal_btn_x + reveal_btn_w &&
                y >= reveal_btn_y && y <= reveal_btn_y + reveal_btn_h) {
                reveal = !reveal;
                draw_reveal_button();
                redraw_input();
                continue;
            }

            for (auto& k : keys) {
                if (x >= k.x && x <= k.x + k.w && y >= k.y && y <= k.y + k.h) {
                    switch (k.action) {
                        case KEY_CHAR:
                            if (value.length() < 64) {
                                value += k.ch;
                                redraw_input();
                            }
                            break;
                        case KEY_SHIFT:
                            shift = !shift;
                            draw_keyboard();
                            break;
                        case KEY_MODE:
                            numeric = !numeric;
                            shift = false;
                            draw_keyboard();
                            break;
                        case KEY_BACKSPACE:
                            if (value.length() > 0) {
                                value.remove(value.length() - 1);
                                redraw_input();
                            }
                            break;
                        case KEY_SPACE:
                            if (value.length() < 64) {
                                value += ' ';
                                redraw_input();
                            }
                            break;
                        case KEY_OK:
                            Serial.printf("[SetupGUI] Input confirmed for '%s' (len=%d)\n", title,
                                          (int)value.length());
                            return value;
                    }
                    break;
                }
            }
        }
        delay(20);
    }
}

// =======================================================================
// Screen 3: Hub IP address input (numeric keypad)
// =======================================================================

static String ip_input(const char* title, const String& initial) {
    String value = initial;
    int w = M5.Display.width();
    int h = M5.Display.height();

    static const int NK_W = 70;
    static const int NK_H = 32;
    static const int NK_GAP = 4;
    static const int NK_Y = 90;
    int nk_x0 = (w - (4 * NK_W + 3 * NK_GAP)) / 2;

    struct NKey {
        int x, y, w, h;
        String label;
        char ch;  // 0 = special
        bool is_bksp;
        bool is_ok;
    };

    auto build_keys = [&](std::vector<NKey>& keys) {
        keys.clear();
        const char digits[] = "123456789";
        for (int r = 0; r < 3; r++) {
            for (int c = 0; c < 3; c++) {
                NKey k;
                k.x = nk_x0 + c * (NK_W + NK_GAP);
                k.y = NK_Y + r * (NK_H + NK_GAP);
                k.w = NK_W;
                k.h = NK_H;
                k.ch = digits[r * 3 + c];
                k.label = String(k.ch);
                k.is_bksp = false;
                k.is_ok = false;
                keys.push_back(k);
            }
            NKey side;
            side.x = nk_x0 + 3 * (NK_W + NK_GAP);
            side.y = NK_Y + r * (NK_H + NK_GAP);
            side.w = NK_W;
            side.h = NK_H;
            if (r == 0) {
                side.label = "Bksp";
                side.ch = 0;
                side.is_bksp = true;
                side.is_ok = false;
            } else if (r == 1) {
                side.label = ".";
                side.ch = '.';
                side.is_bksp = false;
                side.is_ok = false;
            } else {
                side.label = "";
                side.ch = 0;
                side.is_bksp = false;
                side.is_ok = false;
            }
            if (side.label.length() > 0) keys.push_back(side);
        }
        int bottom_y = NK_Y + 3 * (NK_H + NK_GAP);
        int half_w = (4 * NK_W + 3 * NK_GAP - NK_GAP) / 2;
        NKey zero;
        zero.x = nk_x0;
        zero.y = bottom_y;
        zero.w = half_w;
        zero.h = NK_H;
        zero.ch = '0';
        zero.label = "0";
        zero.is_bksp = false;
        zero.is_ok = false;
        keys.push_back(zero);

        NKey ok;
        ok.x = nk_x0 + half_w + NK_GAP;
        ok.y = bottom_y;
        ok.w = half_w;
        ok.h = NK_H;
        ok.ch = 0;
        ok.label = "OK";
        ok.is_bksp = false;
        ok.is_ok = true;
        keys.push_back(ok);
    };

    std::vector<NKey> keys;
    build_keys(keys);

    auto redraw_input = [&]() {
        M5.Display.fillRect(0, INPUT_Y, w, INPUT_H, TFT_BLACK);
        M5.Display.drawRect(0, INPUT_Y, w, INPUT_H, TFT_WHITE);
        M5.Display.setTextSize(2);
        String display_text = value;
        int visible_w = w - 12;
        while (M5.Display.textWidth(display_text) > visible_w && display_text.length() > 0) {
            display_text = display_text.substring(1);
        }
        M5.Display.setTextDatum(textdatum_t::middle_left);
        M5.Display.setTextColor(TFT_WHITE);
        M5.Display.drawString(display_text, 6, INPUT_Y + INPUT_H / 2);
    };

    auto full_redraw = [&]() {
        M5.Display.fillScreen(TFT_BLACK);
        M5.Display.setTextDatum(textdatum_t::top_left);
        M5.Display.setTextSize(1);
        M5.Display.setTextColor(TFT_LIGHTGREY);
        M5.Display.drawString(title, 4, 1);

        M5.Display.setTextSize(1);
        M5.Display.setTextColor(TFT_DARKGREY);
        static const String hub_url_hint = "ws://<IP>:" + String(HUB_WS_PORT) + "/ws/device";
        M5.Display.drawString(hub_url_hint, 4, INPUT_Y + INPUT_H + 4);

        redraw_input();

        for (auto& k : keys) {
            uint16_t fill = k.is_ok ? TFT_BLUE : TFT_DARKGREY;
            M5.Display.fillRect(k.x, k.y, k.w, k.h, fill);
            M5.Display.drawRect(k.x, k.y, k.w, k.h, TFT_WHITE);
            M5.Display.setTextDatum(textdatum_t::middle_center);
            M5.Display.setTextSize(2);
            M5.Display.setTextColor(TFT_WHITE);
            M5.Display.drawString(k.label, k.x + k.w / 2, k.y + k.h / 2);
        }
    };

    full_redraw();

    while (true) {
        M5.update();
        auto t = M5.Touch.getDetail();
        if (t.wasPressed()) {
            int tx = t.x;
            int ty = t.y;
            for (auto& k : keys) {
                if (tx >= k.x && tx <= k.x + k.w && ty >= k.y && ty <= k.y + k.h) {
                    if (k.is_ok) {
                        Serial.printf("[SetupGUI] Hub IP confirmed: %s\n", value.c_str());
                        return value;
                    } else if (k.is_bksp) {
                        if (value.length() > 0) {
                            value.remove(value.length() - 1);
                            redraw_input();
                        }
                    } else if (k.ch != 0) {
                        if (value.length() < 15) {
                            value += k.ch;
                            redraw_input();
                        }
                    }
                    break;
                }
            }
        }
        delay(20);
    }
}

// =======================================================================
// Screen 4: connection test
// =======================================================================

static bool run_connection_test(const String& ssid, const String& password) {
    int w = M5.Display.width();

    M5.Display.fillScreen(TFT_BLACK);
    M5.Display.setTextDatum(textdatum_t::top_left);
    M5.Display.setTextSize(2);
    M5.Display.setTextColor(TFT_WHITE);
    M5.Display.drawString("Connecting to WiFi...", 10, 10);
    M5.Display.setTextSize(1);
    M5.Display.drawString(ssid, 10, 40);

    Serial.printf("[SetupGUI] Connecting to WiFi SSID=%s\n", ssid.c_str());
    WiFi.mode(WIFI_STA);
    WiFi.begin(ssid.c_str(), password.c_str());

    const unsigned long CONNECT_TIMEOUT_MS = 15000;
    unsigned long start = millis();
    int dots = 0;
    int dot_y = 60;

    while (millis() - start < CONNECT_TIMEOUT_MS) {
        if (WiFi.status() == WL_CONNECTED) {
            Serial.println();
            return true;
        }
        delay(300);
        Serial.print(".");
        dots = (dots + 1) % 20;
        String dot_line;
        for (int i = 0; i < dots; i++) dot_line += '.';
        M5.Display.fillRect(10, dot_y, w - 20, 20, TFT_BLACK);
        M5.Display.setTextDatum(textdatum_t::top_left);
        M5.Display.drawString(dot_line, 10, dot_y);
    }
    Serial.println();
    return WiFi.status() == WL_CONNECTED;
}

static void draw_connected_screen() {
    int w = M5.Display.width();

    M5.Display.fillScreen(TFT_BLACK);
    M5.Display.setTextDatum(textdatum_t::middle_center);
    M5.Display.setTextSize(2);
    M5.Display.setTextColor(TFT_GREEN);
    M5.Display.drawString("Connected!", w / 2, 80);
    M5.Display.setTextSize(1);
    M5.Display.setTextColor(TFT_WHITE);
    M5.Display.drawString(WiFi.localIP().toString(), w / 2, 120);
}

static void draw_failed_screen_and_wait_back() {
    int w = M5.Display.width();

    M5.Display.fillScreen(TFT_BLACK);
    M5.Display.setTextDatum(textdatum_t::middle_center);
    M5.Display.setTextSize(2);
    M5.Display.setTextColor(TFT_RED);
    M5.Display.drawString("Connection failed!", w / 2, 80);

    int btn_w = 120;
    int btn_h = 40;
    int btn_x = (w - btn_w) / 2;
    int btn_y = 150;
    M5.Display.fillRect(btn_x, btn_y, btn_w, btn_h, TFT_DARKGREY);
    M5.Display.drawRect(btn_x, btn_y, btn_w, btn_h, TFT_WHITE);
    M5.Display.setTextSize(1);
    M5.Display.setTextColor(TFT_WHITE);
    M5.Display.drawString("Back", btn_x + btn_w / 2, btn_y + btn_h / 2);

    while (true) {
        M5.update();
        auto t = M5.Touch.getDetail();
        if (t.wasPressed()) {
            if (t.x >= btn_x && t.x <= btn_x + btn_w && t.y >= btn_y && t.y <= btn_y + btn_h) {
                Serial.println("[SetupGUI] Back tapped after failed connection");
                return;
            }
        }
        delay(20);
    }
}

// =======================================================================
// Public entry point
// =======================================================================

SetupResult setup_gui_run() {
    Serial.printf("[SetupGUI] Display %dx%d\n", M5.Display.width(), M5.Display.height());
    bool has_existing = config_store_has_config();

    while (true) {
        WifiSelectResult sel = run_wifi_select_screen(has_existing);
        if (sel.cancelled) {
            Serial.println("[SetupGUI] Setup cancelled - returning existing config");
            SetupResult r;
            r.ssid = config_store_get_ssid();
            r.password = config_store_get_password();
            r.hub_url = config_store_get_hub_url();
            r.completed = false;
            return r;
        }

        String ssid = sel.ssid;
        String password = keyboard_input("WiFi Password", true, "");

        bool ok = run_connection_test(ssid, password);
        if (!ok) {
            draw_failed_screen_and_wait_back();
            continue;
        }

        draw_connected_screen();
        delay(1500);

        String default_ip = WiFi.gatewayIP().toString();
        String hub_ip = ip_input("Hub IP Address", default_ip);
        String hub_url = "ws://" + hub_ip + ":" + String(HUB_WS_PORT) + "/ws/device";

        config_store_save(ssid, password, hub_url);
        SetupResult r;
        r.ssid = ssid;
        r.password = password;
        r.hub_url = hub_url;
        r.completed = true;
        return r;
    }
}
