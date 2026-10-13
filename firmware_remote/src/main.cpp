// ---------------------------------------------------------------------------
// firmware_remote — M5StickC Plus + Hat MiniJoyC の ESP-NOW リモコン
//
// MiniJoyC を 20 Hz で読み、正規化した x/y/btn を ESP-NOW で StackChanMove
// (CoreS3) に直接送る。Hub は経由しない。中立が続く間は 5 Hz のハートビートに
// 落とすが、端末側のデッドマン判定の根拠になるため送信は決して止めない。
//
// このファイルはオーケストレータに徹する:
//   joy_input — MiniJoyC の読み取り・校正・正規化・LED
//   joy_link  — ESP-NOW の探索 (チャネルホッピング) / ピア保存 / 送信
//   joy_ui    — LCD 表示
// ---------------------------------------------------------------------------

#include <Arduino.h>
#include <M5Unified.h>

#include "config.h"
#include "joy_input.h"
#include "joy_link.h"
#include "joy_protocol.h"
#include "joy_ui.h"

// MiniJoyC RGB LED の色 (RGB888)
static constexpr uint32_t kLedOff = 0x000000u;
static constexpr uint32_t kLedBlue = 0x0040FFu;    // 探索中 (点滅)
static constexpr uint32_t kLedGreen = 0x00C832u;   // リンク中
static constexpr uint32_t kLedRed = 0xFF0000u;     // ボタン押下
static constexpr uint32_t kLedOrange = 0xFF8000u;  // 低電圧
static constexpr uint32_t kLedBlinkMs = 500;

static constexpr unsigned long kI2cRetryMs = 1000;
static constexpr unsigned long kBatteryPollMs = 1000;

static uint16_t s_seq = 0;
static unsigned long s_last_poll = 0;
static unsigned long s_last_send = 0;
static unsigned long s_last_active = 0;  // 最後に中立でなかった時刻
static unsigned long s_last_diag = 0;
static unsigned long s_last_bat = 0;
static unsigned long s_last_i2c_retry = 0;
static bool s_repair_fired = false;
static bool s_link_ok = false;
static uint16_t s_vbat_mv = 0;
static JoyReading s_reading;

static bool low_battery() {
    return s_vbat_mv != 0 && s_vbat_mv < JOY_LOW_BAT_MV;
}

static void update_led() {
    uint32_t color;
    if (s_reading.btn) {
        color = kLedRed;
    } else if (low_battery()) {
        color = kLedOrange;  // 低電圧は緑より優先する
    } else if (joy_link_state() == JOY_LINK_LINKED) {
        color = kLedGreen;
    } else {
        color = ((millis() / kLedBlinkMs) % 2) ? kLedBlue : kLedOff;
    }
    joy_input_set_led(color);
}

static void send_joy_packet() {
    joyp::Packet p;
    p.type = joyp::TYPE_JOY;
    p.seq = s_seq++;
    p.x = joyp::axis_to_wire(s_reading.x);
    p.y = joyp::axis_to_wire(s_reading.y);
    p.btn = s_reading.btn ? 1 : 0;
    p.flags = 0;
    if (joy_input_calibrated()) {
        p.flags |= joyp::FLAG_CALIBRATED;
    }
    if (low_battery()) {
        p.flags |= joyp::FLAG_LOW_BAT;
    }
    p.aux = s_vbat_mv;
    p.chan = 0;
    joy_link_send_joy(p);
}

static void update_ui() {
    JoyUiModel m;
    m.state = joy_link_state_name();
    m.chan = joy_link_channel();
    m.mac = joy_link_has_peer() ? joy_link_peer_mac() : nullptr;
    m.x = s_reading.x;
    m.y = s_reading.y;
    m.btn = s_reading.btn;
    m.joy_ok = joy_input_ready();
    m.vbat_mv = s_vbat_mv;
    m.low_bat = low_battery();
    const JoyLinkStats& st = joy_link_stats();
    m.tx_total = st.tx_total;
    m.tx_ok = st.tx_ok;
    m.tx_fail = st.tx_fail;
    joy_ui_update(m);
}

void setup() {
    auto cfg = M5.config();
    cfg.serial_baudrate = 115200;
    M5.begin(cfg);
    Serial.begin(115200);
    joy_ui_begin();
    Serial.println("[Joy] StackChanMove remote boot");

    if (joy_input_begin()) {
        joy_input_calibrate();
    } else {
        joy_ui_error("MiniJoyC", "not found - retrying");
    }

    s_link_ok = joy_link_begin();
    if (!s_link_ok) {
        // ESP-NOW が初期化できない場合は送信手段が無い。誤解を招かないよう
        // 通常表示に戻さず、エラー表示のまま据え置く。
        joy_ui_error("ESP-NOW", "init failed");
    }

    s_vbat_mv = static_cast<uint16_t>(M5.Power.getBatteryVoltage());
    const unsigned long now = millis();
    s_last_active = now;
    s_last_bat = now;
    s_last_diag = now;
}

void loop() {
    M5.update();
    const unsigned long now = millis();

    // --- ボタン (駆動には一切影響しない) ---------------------
    if (M5.BtnA.pressedFor(JOY_REPAIR_HOLD_MS)) {
        if (!s_repair_fired) {
            s_repair_fired = true;
            joy_link_repair();
        }
    } else if (!M5.BtnA.isPressed()) {
        s_repair_fired = false;
    }
    if (M5.BtnB.wasPressed()) {
        joy_ui_next_page();
    }

    // --- MiniJoyC が見つからない間は 1 秒ごとに再初期化を試す -----------
    if (!joy_input_ready() && now - s_last_i2c_retry >= kI2cRetryMs) {
        s_last_i2c_retry = now;
        if (joy_input_begin()) {
            joy_input_calibrate();
        } else {
            joy_ui_error("MiniJoyC", "not found - retrying");
        }
    }

    // --- ESP-NOW 探索 / リンク断判定 ------------------------------------
    joy_link_update();

    if (now - s_last_bat >= kBatteryPollMs) {
        s_last_bat = now;
        const int32_t mv = M5.Power.getBatteryVoltage();
        s_vbat_mv = mv > 0 ? static_cast<uint16_t>(mv) : 0;
    }

    // --- 読み取り (20 Hz) と送信 (中立が続けば 5 Hz) --------------------
    if (now - s_last_poll >= JOY_POLL_MS) {
        s_last_poll = now;
        s_reading = joy_input_read();
        const bool neutral = (s_reading.x == 0.0f && s_reading.y == 0.0f && !s_reading.btn);
        if (!neutral) {
            s_last_active = now;
        }
        const unsigned long interval =
            (now - s_last_active >= JOY_IDLE_HB_MS) ? JOY_IDLE_SEND_MS : JOY_POLL_MS;
        if (now - s_last_send >= interval) {
            s_last_send = now;
            send_joy_packet();
        }
        update_led();
    }

    if (s_link_ok) {
        update_ui();
    } else {
        joy_ui_error("ESP-NOW", "init failed");
    }

    if (now - s_last_diag >= JOY_DIAG_INTERVAL_MS) {
        s_last_diag = now;
        const JoyLinkStats& st = joy_link_stats();
        Serial.printf(
            "[Joy] state=%s ch=%u/%u raw=(%d,%d) x=%.2f y=%.2f btn=%d disc=%u ack=%u tx=%u ok=%u "
            "fail=%u cherr=%u lost=%u relink=%u vbat=%u\n",
            joy_link_state_name(), static_cast<unsigned>(joy_link_channel()),
            static_cast<unsigned>(joy_link_radio_channel()), static_cast<int>(s_reading.raw_x),
            static_cast<int>(s_reading.raw_y), s_reading.x, s_reading.y, s_reading.btn ? 1 : 0,
            static_cast<unsigned>(st.discover_tx), static_cast<unsigned>(st.ack_rx),
            static_cast<unsigned>(st.tx_total), static_cast<unsigned>(st.tx_ok),
            static_cast<unsigned>(st.tx_fail), static_cast<unsigned>(st.chan_err),
            static_cast<unsigned>(st.lost_events), static_cast<unsigned>(st.relink_joy),
            static_cast<unsigned>(s_vbat_mv));
    }
}
