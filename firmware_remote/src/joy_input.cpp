#include "joy_input.h"

#include <Arduino.h>
#include <M5HatMiniJoyC.h>
#include <Wire.h>

#include "config.h"
#include "joy_math.h"

// ---------------------------------------------------------------------------
// 内部状態
// ---------------------------------------------------------------------------
static M5HatMiniJoyC s_joy;
static bool s_ready = false;
static bool s_calibrated = false;
// MiniJoyC の POS_VALUE_REG_10_BIT は符号付き (−512..511、中立 ≈ 0)。ライブラリは
// uint16_t で返す (中立 −18 が 65518 に見える) ので int16_t にキャストして扱う。
static float s_center_x = 0.0f;
static float s_center_y = 0.0f;

static int16_t read_pos(minijoyc_pos_index_t idx) {
    return static_cast<int16_t>(s_joy.getPOSValue(idx, _10bit));
}
static uint8_t s_fw = 0;
static uint32_t s_led = 0xFFFFFFFFu;  // 未設定を表す番兵 (RGB888 は 24 bit)

static constexpr float kHalfRange = 512.0f;  // 10 bit POS (0..1023) の片側振れ幅

/// I2C バス復旧: 転送の途中でリセットがかかると MiniJoyC (STM32) が SDA を Low に
/// 保持したままになることがある。SDA = GPIO0 のため、その状態でもう一度リセットされると
/// ESP32 がダウンロードモード (boot:0x3) に入って起動しない。起動時に SCL を 9 回
/// 叩いてスレーブのシフトを流し、STOP を出してバスを解放しておく。
static void i2c_bus_recover() {
    pinMode(JOY_I2C_SDA, INPUT_PULLUP);
    pinMode(JOY_I2C_SCL, OUTPUT_OPEN_DRAIN);
    digitalWrite(JOY_I2C_SCL, HIGH);
    delayMicroseconds(5);
    if (digitalRead(JOY_I2C_SDA) == LOW) {
        for (int i = 0; i < 9 && digitalRead(JOY_I2C_SDA) == LOW; ++i) {
            digitalWrite(JOY_I2C_SCL, LOW);
            delayMicroseconds(5);
            digitalWrite(JOY_I2C_SCL, HIGH);
            delayMicroseconds(5);
        }
        Serial.printf("[Joy] i2c bus recovered (SDA %s)\n",
                      digitalRead(JOY_I2C_SDA) == LOW ? "still LOW" : "released");
    }
    // STOP: SDA Low → High (SCL High のまま)
    pinMode(JOY_I2C_SDA, OUTPUT_OPEN_DRAIN);
    digitalWrite(JOY_I2C_SDA, LOW);
    delayMicroseconds(5);
    digitalWrite(JOY_I2C_SDA, HIGH);
    delayMicroseconds(5);
    pinMode(JOY_I2C_SDA, INPUT_PULLUP);
    pinMode(JOY_I2C_SCL, INPUT_PULLUP);
}

bool joy_input_begin() {
    i2c_bus_recover();
    s_ready = s_joy.begin(&Wire, JOY_I2C_ADDR, JOY_I2C_SDA, JOY_I2C_SCL, JOY_I2C_SPEED);
    if (!s_ready) {
        Serial.printf("[Joy] MiniJoyC not found at addr=0x%02X (SDA=%d SCL=%d)\n", JOY_I2C_ADDR,
                      JOY_I2C_SDA, JOY_I2C_SCL);
        return false;
    }
    // M5HatMiniJoyC::begin() は速度を uint8_t メンバに切り詰めて Wire に渡すため
    // (ライブラリ側の既知の不具合)、ここで正しいクロックに設定し直す。
    Wire.setClock(JOY_I2C_SPEED);
    s_led = 0xFFFFFFFFu;  // 再初期化後は LED を必ず書き直す
    s_fw = s_joy.getFirmwareVersion();
    Serial.printf("[Joy] MiniJoyC fw=%u addr=0x%02X\n", static_cast<unsigned>(s_fw), JOY_I2C_ADDR);
    return true;
}

bool joy_input_ready() {
    return s_ready;
}

void joy_input_calibrate() {
    if (!s_ready) {
        s_calibrated = false;
        return;
    }
#if JOY_USE_HAT_CAL
    // MiniJoyC 内蔵の校正値 (CAL_REG) をそのまま中央として使う。
    s_center_x = static_cast<float>(static_cast<int16_t>(s_joy.getCalValue(Cal_X_cen)));
    s_center_y = static_cast<float>(static_cast<int16_t>(s_joy.getCalValue(Cal_Y_cen)));
#else
    int32_t sum_x = 0;
    int32_t sum_y = 0;
    for (int i = 0; i < JOY_CENTER_SAMPLES; ++i) {
        sum_x += read_pos(POS_X);
        sum_y += read_pos(POS_Y);
        delay(5);
    }
    s_center_x = static_cast<float>(sum_x) / static_cast<float>(JOY_CENTER_SAMPLES);
    s_center_y = static_cast<float>(sum_y) / static_cast<float>(JOY_CENTER_SAMPLES);
#endif
    s_calibrated = true;
    Serial.printf("[Joy] center cal x=%.1f y=%.1f\n", s_center_x, s_center_y);
}

bool joy_input_calibrated() {
    return s_calibrated;
}

JoyReading joy_input_read() {
    JoyReading r;
    if (!s_ready) {
        return r;
    }
    r.raw_x = read_pos(POS_X);
    r.raw_y = read_pos(POS_Y);
    // getButtonStatus() は BUTTON_REG の生値をそのまま bool 化して返す。
    // 押下で 0 (アクティブ Low) の個体は JOY_BTN_ACTIVE_LOW で吸収する。
    const bool raw_btn = s_joy.getButtonStatus();
#if JOY_BTN_ACTIVE_LOW
    r.btn = !raw_btn;
#else
    r.btn = raw_btn;
#endif

    float x = joym::axis_from_pos(static_cast<float>(r.raw_x), s_center_x, kHalfRange);
    float y = joym::axis_from_pos(static_cast<float>(r.raw_y), s_center_y, kHalfRange);
#if JOY_INVERT_X
    x = -x;
#endif
#if JOY_INVERT_Y
    y = -y;
#endif
    joym::shape_axes(&x, &y, JOY_DEADZONE, JOY_EXPO);
    r.x = x;
    r.y = y;
    r.valid = true;
    return r;
}

void joy_input_set_led(uint32_t rgb888) {
    if (!s_ready || rgb888 == s_led) {
        return;
    }
    s_led = rgb888;
    s_joy.setLEDColor(rgb888);
}

uint8_t joy_input_fw_version() {
    return s_fw;
}

float joy_input_center_x() {
    return s_center_x;
}

float joy_input_center_y() {
    return s_center_y;
}
