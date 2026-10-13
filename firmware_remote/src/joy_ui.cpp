#include "joy_ui.h"

#include <Arduino.h>
#include <M5Unified.h>
#include <string.h>

#include "config.h"

// ---------------------------------------------------------------------------
// 内部状態
// ---------------------------------------------------------------------------
static constexpr uint8_t kPageCount = 2;
static constexpr uint32_t kRedrawIntervalMs = 100;  // 10 Hz (LCD の負荷を抑える)

static uint8_t s_page = 0;
static bool s_full_redraw = true;
static bool s_error_shown = false;
static unsigned long s_last_draw = 0;
static char s_last_state[8] = {0};

static uint16_t s_col_bg = 0;
static uint16_t s_col_fg = 0;
static uint16_t s_col_dim = 0;
static uint16_t s_col_green = 0;
static uint16_t s_col_blue = 0;
static uint16_t s_col_red = 0;
static uint16_t s_col_orange = 0;

static uint16_t state_color(const char* state) {
    if (strcmp(state, "LINKED") == 0) return s_col_green;
    if (strcmp(state, "LOST") == 0) return s_col_red;
    return s_col_blue;
}

/// 中央基準の水平バー (値域 [-1, 1])。枠内を毎回塗り直すのでちらつかない。
static void draw_bar(int y, float v, uint16_t color) {
    constexpr int kX = 4;
    constexpr int kW = 127;
    constexpr int kH = 12;
    const int center = kX + kW / 2;
    M5.Display.drawRect(kX, y, kW, kH, s_col_dim);
    M5.Display.fillRect(kX + 1, y + 1, kW - 2, kH - 2, s_col_bg);
    M5.Display.drawFastVLine(center, y + 1, kH - 2, s_col_dim);
    if (v > 1.0f) v = 1.0f;
    if (v < -1.0f) v = -1.0f;
    const int len = static_cast<int>(v * static_cast<float>(kW / 2 - 2));
    if (len > 0) {
        M5.Display.fillRect(center + 1, y + 2, len, kH - 4, color);
    } else if (len < 0) {
        M5.Display.fillRect(center + len, y + 2, -len, kH - 4, color);
    }
}

void joy_ui_begin() {
    M5.Display.setRotation(0);  // 135x240 縦
    s_col_bg = M5.Display.color565(0, 0, 0);
    s_col_fg = M5.Display.color565(255, 255, 255);
    s_col_dim = M5.Display.color565(110, 110, 110);
    s_col_green = M5.Display.color565(0, 220, 60);
    s_col_blue = M5.Display.color565(60, 140, 255);
    s_col_red = M5.Display.color565(255, 60, 60);
    s_col_orange = M5.Display.color565(255, 160, 0);
    M5.Display.fillScreen(s_col_bg);
    M5.Display.setTextColor(s_col_fg, s_col_bg);
    M5.Display.setTextSize(1);
    s_full_redraw = true;
}

void joy_ui_next_page() {
    s_page = static_cast<uint8_t>((s_page + 1) % kPageCount);
    s_full_redraw = true;
}

uint8_t joy_ui_page() {
    return s_page;
}

void joy_ui_error(const char* line1, const char* line2) {
    if (s_error_shown) {
        return;
    }
    s_error_shown = true;
    M5.Display.fillScreen(s_col_bg);
    M5.Display.setTextColor(s_col_red, s_col_bg);
    M5.Display.setTextSize(2);
    M5.Display.setCursor(4, 40);
    M5.Display.print(line1 ? line1 : "");
    M5.Display.setTextSize(1);
    M5.Display.setCursor(4, 76);
    M5.Display.print(line2 ? line2 : "");
    M5.Display.setTextColor(s_col_fg, s_col_bg);
}

static void draw_page_main(const JoyUiModel& m) {
    M5.Display.setTextSize(2);
    M5.Display.setTextColor(state_color(m.state), s_col_bg);
    M5.Display.setCursor(4, 4);
    M5.Display.printf("%-7s", m.state);

    M5.Display.setTextSize(1);
    M5.Display.setTextColor(s_col_fg, s_col_bg);
    M5.Display.setCursor(4, 26);
    if (m.mac) {
        M5.Display.printf("ch=%-2u %02X:%02X    ", m.chan, m.mac[4], m.mac[5]);
    } else {
        M5.Display.printf("ch=%-2u --:--    ", m.chan);
    }

    M5.Display.setCursor(4, 42);
    M5.Display.printf("X %+.2f  ", m.x);
    draw_bar(54, m.x, s_col_green);

    M5.Display.setCursor(4, 74);
    M5.Display.printf("Y %+.2f  ", m.y);
    draw_bar(86, m.y, s_col_green);

    M5.Display.setTextSize(2);
    M5.Display.setTextColor(m.btn ? s_col_red : s_col_dim, s_col_bg);
    M5.Display.setCursor(4, 110);
    M5.Display.print("BTN");
    M5.Display.setTextSize(1);
    M5.Display.setTextColor(s_col_fg, s_col_bg);

    M5.Display.setTextColor(m.low_bat ? s_col_orange : s_col_fg, s_col_bg);
    M5.Display.setCursor(4, 142);
    M5.Display.printf("BAT %4u mV ", m.vbat_mv);
    M5.Display.setTextColor(s_col_fg, s_col_bg);

    const uint32_t done = m.tx_ok + m.tx_fail;
    const unsigned pct = done ? static_cast<unsigned>((m.tx_ok * 100 + done / 2) / done) : 0;
    M5.Display.setCursor(4, 158);
    M5.Display.printf("TX %6lu   ", static_cast<unsigned long>(m.tx_total));
    M5.Display.setCursor(4, 172);
    M5.Display.printf("OK %3u%%     ", pct);

    M5.Display.setTextColor(m.joy_ok ? s_col_dim : s_col_red, s_col_bg);
    M5.Display.setCursor(4, 194);
    M5.Display.print(m.joy_ok ? "JoyC ok    " : "JoyC ERROR ");
    M5.Display.setTextColor(s_col_dim, s_col_bg);
    M5.Display.setCursor(4, 212);
    M5.Display.print("A:repair B:page");
    M5.Display.setTextColor(s_col_fg, s_col_bg);
}

static void draw_page_stats(const JoyUiModel& m) {
    M5.Display.setTextSize(2);
    M5.Display.setTextColor(state_color(m.state), s_col_bg);
    M5.Display.setCursor(4, 4);
    M5.Display.printf("%-7s", m.state);

    M5.Display.setTextSize(1);
    M5.Display.setTextColor(s_col_fg, s_col_bg);
    M5.Display.setCursor(4, 30);
    M5.Display.print("PEER");
    M5.Display.setCursor(4, 44);
    if (m.mac) {
        M5.Display.printf("%02X:%02X:%02X", m.mac[0], m.mac[1], m.mac[2]);
        M5.Display.setCursor(4, 56);
        M5.Display.printf("%02X:%02X:%02X", m.mac[3], m.mac[4], m.mac[5]);
    } else {
        M5.Display.print("(none)   ");
        M5.Display.setCursor(4, 56);
        M5.Display.print("         ");
    }
    M5.Display.setCursor(4, 74);
    M5.Display.printf("chan  %-3u", m.chan);
    M5.Display.setCursor(4, 92);
    M5.Display.printf("tx    %-8lu", static_cast<unsigned long>(m.tx_total));
    M5.Display.setCursor(4, 106);
    M5.Display.printf("ok    %-8lu", static_cast<unsigned long>(m.tx_ok));
    M5.Display.setCursor(4, 120);
    M5.Display.printf("fail  %-8lu", static_cast<unsigned long>(m.tx_fail));
    M5.Display.setCursor(4, 138);
    M5.Display.printf("x %+.2f   ", m.x);
    M5.Display.setCursor(4, 152);
    M5.Display.printf("y %+.2f   ", m.y);
    M5.Display.setCursor(4, 166);
    M5.Display.printf("btn %d     ", m.btn ? 1 : 0);
    M5.Display.setCursor(4, 184);
    M5.Display.printf("bat %4u mV", m.vbat_mv);
    M5.Display.setTextColor(s_col_dim, s_col_bg);
    M5.Display.setCursor(4, 212);
    M5.Display.print("A:repair B:page");
    M5.Display.setTextColor(s_col_fg, s_col_bg);
}

void joy_ui_update(const JoyUiModel& m) {
    const unsigned long now = millis();
    if (s_error_shown) {
        if (!m.joy_ok) {
            return;  // 復帰するまでエラー表示を維持する
        }
        s_error_shown = false;
        s_full_redraw = true;
    }
    if (!s_full_redraw && now - s_last_draw < kRedrawIntervalMs) {
        return;
    }
    if (strncmp(s_last_state, m.state, sizeof(s_last_state) - 1) != 0) {
        snprintf(s_last_state, sizeof(s_last_state), "%s", m.state);
        s_full_redraw = true;
    }
    if (s_full_redraw) {
        M5.Display.fillScreen(s_col_bg);
        s_full_redraw = false;
    }
    s_last_draw = now;

    if (s_page == 0) {
        draw_page_main(m);
    } else {
        draw_page_stats(m);
    }
}
