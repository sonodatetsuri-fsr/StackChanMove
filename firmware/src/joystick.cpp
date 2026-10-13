#include "joystick.h"

#include <Arduino.h>
#include <math.h>
#include <stdio.h>
#include <string.h>

#include "config.h"
#include "drive_controller.h"
#include "joy_math.h"
#include "joystick_link.h"

// ---------------------------------------------------------------------------
// 状態。すべて loop() (単一タスク) からのみ触る。
// ---------------------------------------------------------------------------
static constexpr size_t kMsgLen = 160;
static constexpr size_t kQueueLen = 6;

static char s_queue[kQueueLen][kMsgLen];
static size_t s_q_head = 0;  // 取り出し位置
static size_t s_q_count = 0;

// 最後に受信したスティック値 (リンク中のみ有効)
static float s_x = 0.0f;
static float s_y = 0.0f;
static bool s_btn = false;

static bool s_drive_active = false;  // ローカル指令を出している最中か (リンク断で 0 を 1 回出す)

// ボタンのエッジ / 長押し判定
static bool s_btn_prev = false;
static uint32_t s_btn_down_ms = 0;
static bool s_long_fired = false;
static DriveSource s_src_before_press = DRIVE_SRC_HUB;  // 押下エッジ直前の選択ソース

// Hub への joy{} 送信
static float s_sent_x = 0.0f;
static float s_sent_y = 0.0f;
static bool s_sent_btn = false;
static bool s_sent_any = false;
static uint32_t s_last_send_ms = 0;

/// 送信キューに積む (満杯なら最も古いものを捨てる = 最新を優先)。
static void enqueue(const char* json) {
    if (!json) return;
    if (s_q_count == kQueueLen) {
        s_q_head = (s_q_head + 1) % kQueueLen;
        --s_q_count;
    }
    const size_t tail = (s_q_head + s_q_count) % kQueueLen;
    snprintf(s_queue[tail], kMsgLen, "%s", json);
    ++s_q_count;
}

static void enqueue_event(const char* name, const char* detail) {
    char buf[kMsgLen];
    snprintf(buf, sizeof(buf), "{\"type\":\"event\",\"name\":\"%s\",\"detail\":\"%s\"}", name,
             detail);
    enqueue(buf);
}

static void enqueue_joy(float x, float y, bool btn) {
    char buf[kMsgLen];
    snprintf(buf, sizeof(buf), "{\"type\":\"joy\",\"x\":%.3f,\"y\":%.3f,\"btn\":%s}",
             static_cast<double>(x), static_cast<double>(y), btn ? "true" : "false");
    enqueue(buf);
}

// ---------------------------------------------------------------------------
// ボタン規則
// ---------------------------------------------------------------------------
static void handle_button(bool btn, bool hub_connected) {
    const uint32_t now = millis();
    if (btn && !s_btn_prev) {
        // 押下エッジ = 非常停止。駆動ソースによらず即停止し hub に落とす。
        s_btn_down_ms = now;
        s_long_fired = false;
        s_src_before_press = drive_controller_get_source();
        drive_controller_estop();
        Serial.println("[Joy] estop (button)");
        enqueue_event("estop", "joystick_button");
    } else if (btn && !s_long_fired && (now - s_btn_down_ms) >= JOY_LONG_PRESS_MS) {
        s_long_fired = true;
        // 長押しは Hub 未接続時のみ有効 (Hub 接続中のソース変更権限は Hub にある)。
        // 押下エッジの estop で既に hub に落ちているため、トグルは押下前のソースを基準にする
        // (joystick で運用中に長押し → hub、hub なら → joystick)。
        if (!hub_connected) {
            const bool to_joy = (s_src_before_press != DRIVE_SRC_JOYSTICK);
            drive_controller_set_source(to_joy ? "joystick" : "hub");
            Serial.printf("[Joy] source -> %s (standalone)\n", to_joy ? "joystick" : "hub");
        }
    }
    s_btn_prev = btn;
}

// ---------------------------------------------------------------------------
// API
// ---------------------------------------------------------------------------
void joystick_init() {
    s_q_head = 0;
    s_q_count = 0;
    s_x = 0.0f;
    s_y = 0.0f;
    s_btn = false;
    s_drive_active = false;
    s_btn_prev = false;
    s_long_fired = false;
    s_sent_any = false;
    s_last_send_ms = 0;
}

void joystick_update(bool hub_connected) {
    joystick_link_update();

    // リンク up/down を Hub の event{name:"joy_link"} に載せる。
    bool up = false;
    char mac_str[18];
    if (joystick_link_pop_link_event(&up, mac_str, sizeof(mac_str))) {
        char detail[32];
        if (up) {
            snprintf(detail, sizeof(detail), "up %s", mac_str);
        } else {
            snprintf(detail, sizeof(detail), "down");
        }
        enqueue_event("joy_link", detail);
    }

    const bool linked = joystick_link_is_linked();
    JoyLinkSample s;
    const bool got = joystick_link_take(&s);
    if (got) {
        s_x = s.x;
        s_y = s.y;
        s_btn = s.btn;
    }

    // リンク断中はボタンも離した扱い (長押しを跨いで誤発火させない)。
    handle_button(linked && s_btn, hub_connected);

    // (v, w) のローカル適用: 新しいサンプルごと (≈20 Hz)。
    if (linked) {
        if (got && drive_controller_get_source() == DRIVE_SRC_JOYSTICK) {
            float v = 0.0f, w = 0.0f;
            joym::stick_to_cmd(s_x, s_y, MAX_V_MPS, MAX_W_RADPS, &v, &w);
            drive_controller_set_local_cmd(v, w);
            s_drive_active = true;
        }
    } else if (s_drive_active) {
        // デッドマン: リンク断で 1 回だけ 0 を入れて止める。
        drive_controller_set_local_cmd(0.0f, 0.0f);
        s_drive_active = false;
        s_x = 0.0f;
        s_y = 0.0f;
        s_btn = false;
    }

    // Hub への joy{x, y, btn} (変化 / ハートビート、最大 JOY_SEND_MAX_HZ)。
    if (linked && hub_connected) {
        const uint32_t now = millis();
        const uint32_t min_interval = 1000u / JOY_SEND_MAX_HZ;
        const bool changed = !s_sent_any || fabsf(s_x - s_sent_x) > JOY_SEND_MIN_DELTA ||
                             fabsf(s_y - s_sent_y) > JOY_SEND_MIN_DELTA || (s_btn != s_sent_btn);
        const bool heartbeat = (now - s_last_send_ms) >= JOY_SEND_HEARTBEAT_MS;
        if ((changed || heartbeat) && (now - s_last_send_ms) >= min_interval) {
            enqueue_joy(s_x, s_y, s_btn);
            s_sent_x = s_x;
            s_sent_y = s_y;
            s_sent_btn = s_btn;
            s_sent_any = true;
            s_last_send_ms = now;
        }
    }
}

bool joystick_pop_outgoing(char* buf, size_t len) {
    if (!buf || len == 0 || s_q_count == 0) return false;
    snprintf(buf, len, "%s", s_queue[s_q_head]);
    s_q_head = (s_q_head + 1) % kQueueLen;
    --s_q_count;
    return true;
}

void joystick_diag_report() {
    JoyLinkStats st;
    joystick_link_get_stats(&st);
    char peer[18];
    joystick_link_mac_str(st.has_peer ? joystick_link_peer_mac() : nullptr, peer, sizeof(peer));
    Serial.printf("[Joy] linked=%d rx=%u/s lost=%u last=%ums raw=%u disc=%u ch=%u peer=%s\n",
                  st.linked ? 1 : 0, static_cast<unsigned>(st.rx_per_s),
                  static_cast<unsigned>(st.lost), static_cast<unsigned>(st.last_ms),
                  static_cast<unsigned>(st.raw), static_cast<unsigned>(st.discover),
                  static_cast<unsigned>(st.channel), st.has_peer ? peer : "none");
}
