#include "wheel_driver.h"

#include <Arduino.h>
#include <Preferences.h>
#include <freertos/FreeRTOS.h>
#include <freertos/semphr.h>

#include "config.h"

// StackChan-BSP がベンダリングしているドライバを直接使う (BSP の M5StackChan.begin() は
// 呼ばない = Motion タスクを起動させない)。StackChan-BSP/src がインクルードパスにある。
#include <drivers/FTServo_Arduino/src/SCSCL.h>

#include <drivers/PY32IOExpander/PY32IOExpander.hpp>

// ---------------------------------------------------------------------------
// Module state
// ---------------------------------------------------------------------------
static m5::PY32IOExpander_Class* s_io_expander = nullptr;
static SCSCL s_scs_bus;
static SemaphoreHandle_t s_bus_mutex = nullptr;
static bool s_ok = false;
// 初期化がどの段階で失敗したか (USB CDC が起動直後のログを落とすため定期診断で参照する)。
static const char* s_fail_reason = "not initialized";
// 一度きりのハード初期化 (サーボ電源 / UART ドライバ) は再試行で二度実行しない。
// uart_driver_install は 2 回目に "already installed" で失敗するため (実機 2026-08-27)。
static bool s_power_on = false;
static bool s_uart_begun = false;

static const uint8_t kIdLeft = WHEEL_SERVO_ID_LEFT;
static const uint8_t kIdRight = WHEEL_SERVO_ID_RIGHT;

// ---------------------------------------------------------------------------
// Internal helpers
// ---------------------------------------------------------------------------
static inline bool bus_lock() {
    if (s_bus_mutex == nullptr) return false;
    return xSemaphoreTake(s_bus_mutex, portMAX_DELAY) == pdTRUE;
}

static inline void bus_unlock() {
    if (s_bus_mutex != nullptr) xSemaphoreGive(s_bus_mutex);
}

/// BSP の io_expander_init() と同じ手順でサーボ電源 (VM EN = pin 0) を有効化する。
static bool io_expander_power_on() {
    if (s_power_on && s_io_expander != nullptr) return true;  // 既に投入済み (再試行時)
    if (s_io_expander == nullptr) s_io_expander = new m5::PY32IOExpander_Class();

    // PY32 IO Expander may boot slowly, wait for it
    uint32_t start_tick = millis();
    bool ready = false;
    while (true) {
        delay(200);

        if (millis() - start_tick > 1200) {
            Serial.println("[Wheel] IO expander init timeout");
            s_fail_reason = "io_expander_timeout";
            break;
        }

        if (s_io_expander->begin()) {
            ready = true;
            break;
        }
    }

    if (!ready) {
        delete s_io_expander;
        s_io_expander = nullptr;
        if (s_fail_reason[0] != 'i') s_fail_reason = "io_expander_begin";
        return false;
    }

    s_power_on = true;
    // VM EN
    s_io_expander->setDirection(0, true);  // Output
    s_io_expander->setPullMode(0, true);   // Pull-up
    s_io_expander->digitalWrite(0, true);  // Servo power on
    delay(200);
    return true;
}

/// 模式 2 に切り替える前の角度リミット (EEPROM 9〜12) を初回のみ NVS に保存する。
/// 保存済みの場合は何もしない (0/0 で上書きしないため)。
static void save_angle_limits_once(uint8_t id, int min_limit, int max_limit) {
    if (min_limit < 0 || max_limit < 0) {
        Serial.printf("[Wheel] id=%d angle limit read failed (min=%d max=%d), not saved\n",
                      static_cast<int>(id), min_limit, max_limit);
        return;
    }
    if (min_limit == 0 && max_limit == 0) {
        // 既に模式 2 (PWMMode) のためリミットが 0/0。上書きしない。
        return;
    }

    char min_key[8];
    char max_key[8];
    snprintf(min_key, sizeof(min_key), "min%d", static_cast<int>(id));
    snprintf(max_key, sizeof(max_key), "max%d", static_cast<int>(id));

    Preferences prefs;
    if (!prefs.begin("wheel", false)) {
        Serial.println("[Wheel] NVS open failed, angle limits not saved");
        return;
    }
    if (!prefs.isKey(min_key) || !prefs.isKey(max_key)) {
        prefs.putInt(min_key, min_limit);
        prefs.putInt(max_key, max_limit);
        Serial.printf("[Wheel] saved original angle limits id=%d min=%d max=%d\n",
                      static_cast<int>(id), min_limit, max_limit);
    }
    prefs.end();
}

/// 1 つのサーボを模式 2 (速度開環電機モード) にして停止させる。
static bool setup_servo(uint8_t id) {
    if (s_scs_bus.Ping(id) == -1) {
        Serial.printf("[Wheel] Ping failed id=%d\n", static_cast<int>(id));
        s_fail_reason = (id == kIdLeft) ? "ping_failed_left" : "ping_failed_right";
        return false;
    }

    int min_limit = s_scs_bus.readWord(id, SCSCL_MIN_ANGLE_LIMIT_L);
    int max_limit = s_scs_bus.readWord(id, SCSCL_MAX_ANGLE_LIMIT_L);
    save_angle_limits_once(id, min_limit, max_limit);

    // SwitchMode(id, 1) = PWMMode (角度リミットを 0/0 に書く = 規格書の模式 2)
    int rc = s_scs_bus.SwitchMode(id, 1);
    if (rc != 1) {
        Serial.printf("[Wheel] SwitchMode(PWM) failed id=%d rc=%d\n", static_cast<int>(id), rc);
        s_fail_reason = "switchmode_failed";
        return false;
    }

    s_scs_bus.EnableTorque(id, 1);
    s_scs_bus.WritePWM(id, 0);
    return true;
}

// ---------------------------------------------------------------------------
// Public API
// ---------------------------------------------------------------------------
bool wheel_driver_init() {
    s_ok = false;
    s_fail_reason = "unknown";

    if (s_bus_mutex == nullptr) {
        s_bus_mutex = xSemaphoreCreateMutex();
        if (s_bus_mutex == nullptr) {
            Serial.println("[Wheel] init FAILED (mutex alloc)");
            s_fail_reason = "mutex_alloc";
            return false;
        }
    }

    if (!io_expander_power_on()) {
        Serial.println("[Wheel] init FAILED (IO expander / servo power)");
        return false;
    }

    // UART ドライバは 1 回だけインストールする。再試行時は再インストールしない
    // (uart_driver_install が "already installed" で失敗するため)。サーボ無応答からの
    // 復帰は下の setup_servo (ping / mode) の再実行で行う。
    if (!s_uart_begun) {
        if (!s_scs_bus.begin(WHEEL_UART_NUM, WHEEL_UART_BAUD, WHEEL_UART_TX_PIN,
                             WHEEL_UART_RX_PIN)) {
            Serial.println("[Wheel] init FAILED (UART begin)");
            s_fail_reason = "uart_begin";
            return false;
        }
        s_uart_begun = true;
    }

    bool left_ok = false;
    bool right_ok = false;
    if (bus_lock()) {
        left_ok = setup_servo(kIdLeft);
        right_ok = setup_servo(kIdRight);
        bus_unlock();
    }

    if (!left_ok || !right_ok) {
        Serial.printf("[Wheel] init FAILED (left id=%d %s, right id=%d %s)\n",
                      static_cast<int>(kIdLeft), left_ok ? "ok" : "NG", static_cast<int>(kIdRight),
                      right_ok ? "ok" : "NG");
        return false;
    }

    s_ok = true;
    s_fail_reason = "ok";
    Serial.printf("[Wheel] SCSCL init ok (ids %d,%d mode=2 wheel_d=%.1fmm track=%.1fmm)\n",
                  static_cast<int>(kIdLeft), static_cast<int>(kIdRight),
                  static_cast<double>(WHEEL_DIAMETER_MM), static_cast<double>(TRACK_WIDTH_MM));
    return true;
}

bool wheel_driver_ok() {
    return s_ok;
}

const char* wheel_driver_status_str() {
    return s_ok ? "ok" : s_fail_reason;
}

void wheel_driver_set_pwm(int16_t left, int16_t right) {
    if (!s_ok) return;

    if (left > WHEEL_PWM_MAX) left = WHEEL_PWM_MAX;
    if (left < -WHEEL_PWM_MAX) left = -WHEEL_PWM_MAX;
    if (right > WHEEL_PWM_MAX) right = WHEEL_PWM_MAX;
    if (right < -WHEEL_PWM_MAX) right = -WHEEL_PWM_MAX;

    const int16_t l_out = static_cast<int16_t>(left * WHEEL_LEFT_SIGN);
    const int16_t r_out = static_cast<int16_t>(right * WHEEL_RIGHT_SIGN);

    if (!bus_lock()) return;
    s_scs_bus.WritePWM(kIdLeft, l_out);
    s_scs_bus.WritePWM(kIdRight, r_out);
    bus_unlock();
}

void wheel_driver_stop() {
    if (!s_ok) return;
    if (!bus_lock()) return;
    s_scs_bus.WritePWM(kIdLeft, 0);
    s_scs_bus.WritePWM(kIdRight, 0);
    bus_unlock();
}

bool wheel_driver_read_pos(int* left, int* right) {
    if (left) *left = -1;
    if (right) *right = -1;
    if (!s_ok) return false;
    if (!bus_lock()) return false;

    int l = s_scs_bus.ReadPos(kIdLeft);
    int r = s_scs_bus.ReadPos(kIdRight);
    bus_unlock();

    if (left) *left = l;
    if (right) *right = r;
    return (l != -1) && (r != -1);
}

bool wheel_driver_read_speed(int* left, int* right) {
    if (left) *left = 0;
    if (right) *right = 0;
    if (!s_ok) return false;
    if (!bus_lock()) return false;

    int l = s_scs_bus.ReadSpeed(kIdLeft);
    int r = s_scs_bus.ReadSpeed(kIdRight);
    bus_unlock();

    // readWord 失敗 (−1) は ReadSpeed の符号処理を通ると 32769 になるため、
    // 実機で取り得ない大きさの値もまとめて失敗とみなす。
    const bool l_ok = (l != -1) && (abs(l) <= WHEEL_PWM_MAX * 2);
    const bool r_ok = (r != -1) && (abs(r) <= WHEEL_PWM_MAX * 2);
    if (left) *left = l_ok ? l : 0;
    if (right) *right = r_ok ? r : 0;
    return l_ok && r_ok;
}

int wheel_driver_read_voltage() {
    if (!s_ok) return -1;
    if (!bus_lock()) return -1;
    int v = s_scs_bus.ReadVoltage(kIdLeft);
    bus_unlock();
    return v;
}
