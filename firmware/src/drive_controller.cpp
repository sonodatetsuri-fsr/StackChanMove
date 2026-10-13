#include "drive_controller.h"

#include <Arduino.h>

#include <cstring>

#include "config.h"
#include "diffdrive_math.h"
#include "odometry.h"
#include "wheel_driver.h"

// ---------------------------------------------------------------------------
// 状態。すべて loop() (単一タスク) から触るためロックしない。
// ---------------------------------------------------------------------------
static DriveSource s_source = DRIVE_SRC_JOYSTICK;  // 選択中のソース (既定 = joystick)

static float s_hub_v = 0.0f;
static float s_hub_w = 0.0f;
static DriveSource s_hub_src = DRIVE_SRC_HUB;
static uint32_t s_hub_ms = 0;
static bool s_hub_valid = false;

// 校正用の生 PWM。drive と同じ s_hub_ms / s_hub_valid を共有し、
// 後から来た方 (drive / wheel_pwm) が有効になる。
static int16_t s_hub_pwm_l = 0;
static int16_t s_hub_pwm_r = 0;
static bool s_hub_pwm_active = false;
static uint32_t s_pwm_ignored_log_ms = 0;  // ignored ログのレート制限 (最後に出した時刻)
static bool s_pwm_ignored_logged = false;  // 同上 (初回判定用)
static constexpr uint32_t kPwmIgnoreLogMs = 1000;

static float s_local_v = 0.0f;
static float s_local_w = 0.0f;
static uint32_t s_local_ms = 0;
static bool s_local_valid = false;

static float s_applied_v = 0.0f;
static float s_applied_w = 0.0f;
static DriveSource s_applied_src = DRIVE_SRC_NONE;
static uint32_t s_last_seq = 0;

static int16_t s_pwm_l = 0;
static int16_t s_pwm_r = 0;
static uint32_t s_last_write_ms = 0;

static bool s_timeout_pending = false;  // event{name:"drive_timeout"} 未送信フラグ
static bool s_timeout_logged = false;   // 同じ停止で何度もログ / event を出さないためのラッチ
static bool s_disconnect_logged = false;

// loop() 停止ガード (odometry タスクと共有するため volatile)
static volatile uint32_t s_last_update_ms = 0;
static volatile bool s_stall_stopped = false;
static volatile uint32_t s_stall_ms = 0;
static uint32_t s_stall_count = 0;

static DriveSource parse_source(const char* s, DriveSource fallback) {
    if (!s) return fallback;
    if (strcmp(s, "hub") == 0) return DRIVE_SRC_HUB;
    if (strcmp(s, "joystick") == 0) return DRIVE_SRC_JOYSTICK;
    if (strcmp(s, "auto") == 0) return DRIVE_SRC_AUTO;
    if (strcmp(s, "none") == 0) return DRIVE_SRC_NONE;
    return fallback;
}

static ddm::PwmMap map_left() {
    return ddm::PwmMap{WHEEL_PWM_DEADBAND_L, WHEEL_PWM_PER_RAD_S_L, WHEEL_PWM_MAX,
                       WHEEL_MIN_OMEGA_RADPS};
}

static ddm::PwmMap map_right() {
    return ddm::PwmMap{WHEEL_PWM_DEADBAND_R, WHEEL_PWM_PER_RAD_S_R, WHEEL_PWM_MAX,
                       WHEEL_MIN_OMEGA_RADPS};
}

/// 書き込んだ PWM を写像の逆算で車輪角速度に直し odometry に渡す。
/// 生 PWM (wheel_pwm) も同じ経路を通る。停止は (0, 0)。
static void publish_cmd_omega(int16_t pl, int16_t pr) {
    odometry_set_cmd_omega(ddm::pwm_to_wheel_omega(pl, map_left()),
                           ddm::pwm_to_wheel_omega(pr, map_right()));
}

static void stop_now() {
    s_applied_v = 0.0f;
    s_applied_w = 0.0f;
    s_applied_src = DRIVE_SRC_NONE;
    s_hub_pwm_active = false;
    s_pwm_l = 0;
    s_pwm_r = 0;
    s_last_write_ms = millis();
    wheel_driver_stop();
    publish_cmd_omega(0, 0);
}

// ---------------------------------------------------------------------------
// API
// ---------------------------------------------------------------------------
void drive_controller_init() {
    s_source = DRIVE_SRC_JOYSTICK;
    s_hub_valid = false;
    s_hub_pwm_active = false;
    s_pwm_ignored_logged = false;
    s_local_valid = false;
    s_applied_v = 0.0f;
    s_applied_w = 0.0f;
    s_applied_src = DRIVE_SRC_NONE;
    s_last_seq = 0;
    s_pwm_l = 0;
    s_pwm_r = 0;
    s_last_write_ms = millis();
    s_timeout_pending = false;
    s_timeout_logged = false;
    s_disconnect_logged = false;
    s_last_update_ms = millis();
    s_stall_stopped = false;
}

void drive_controller_set_hub_cmd(float v, float w, uint32_t seq, const char* src) {
    // Hub の drive.src は "hub" (手動) か "auto" (自律)。未知の値は hub 扱い。
    s_hub_src = (src && strcmp(src, "auto") == 0) ? DRIVE_SRC_AUTO : DRIVE_SRC_HUB;
    s_hub_v = v;
    s_hub_w = w;
    s_hub_ms = millis();
    s_hub_valid = (s_source == DRIVE_SRC_HUB || s_source == DRIVE_SRC_AUTO);
    if (s_hub_valid) s_last_seq = seq;
    s_hub_pwm_active = false;  // drive と wheel_pwm は後から来た方が有効
    // 新しい指令が来た = 次のタイムアウトは改めて 1 回だけ通知する。
    s_timeout_logged = false;
    s_disconnect_logged = false;
}

void drive_controller_set_hub_pwm(int l, int r, uint32_t seq) {
    if (s_source != DRIVE_SRC_HUB) {
        // 10 Hz で流れてくるのでシリアルを埋めないよう 1 秒に 1 回だけ出す。
        const uint32_t now = millis();
        if (!s_pwm_ignored_logged || now - s_pwm_ignored_log_ms >= kPwmIgnoreLogMs) {
            Serial.printf("[Drive] wheel_pwm ignored (source=%s)\n", drive_source_str(s_source));
            s_pwm_ignored_log_ms = now;
            s_pwm_ignored_logged = true;
        }
        return;
    }
    s_hub_pwm_l = static_cast<int16_t>(ddm::clamp_pwm(static_cast<int32_t>(l), WHEEL_PWM_MAX));
    s_hub_pwm_r = static_cast<int16_t>(ddm::clamp_pwm(static_cast<int32_t>(r), WHEEL_PWM_MAX));
    s_hub_pwm_active = true;
    s_hub_ms = millis();
    s_hub_valid = true;
    s_last_seq = seq;
    s_timeout_logged = false;
    s_disconnect_logged = false;
    s_pwm_ignored_logged = false;
}

void drive_controller_set_source(const char* source) {
    DriveSource next = parse_source(source, s_source);
    if (next == DRIVE_SRC_NONE) next = DRIVE_SRC_JOYSTICK;
    if (next == s_source) return;
    Serial.printf("[Drive] source %s -> %s\n", drive_source_str(s_source), drive_source_str(next));
    s_source = next;
    // 切替時は一旦停止し、旧ソースの指令を破棄する。
    s_hub_valid = false;
    s_hub_pwm_active = false;
    s_local_valid = false;
    s_timeout_logged = false;
    s_disconnect_logged = false;
    stop_now();
}

void drive_controller_set_local_cmd(float v, float w) {
    s_local_v = v;
    s_local_w = w;
    s_local_ms = millis();
    s_local_valid = (s_source == DRIVE_SRC_JOYSTICK);
}

void drive_controller_estop() {
    // 安全側への遷移は端末が主導する。ソースは hub に落とし、
    // 旧ソースの指令をすべて無効にしてから即停止する。
    s_hub_valid = false;
    s_hub_pwm_active = false;
    s_local_valid = false;
    s_source = DRIVE_SRC_HUB;
    s_timeout_logged = false;
    s_disconnect_logged = false;
    stop_now();
    Serial.println("[Drive] estop -> stop, source hub");
}

DriveSource drive_controller_get_source() {
    return s_source;
}

void drive_controller_on_disconnect() {
    s_hub_valid = false;
    s_hub_pwm_active = false;
    if (!s_disconnect_logged) {
        Serial.println("[Drive] disconnected -> stop");
        s_disconnect_logged = true;
    }
    stop_now();
}

void drive_controller_update() {
    const uint32_t now = millis();
    s_last_update_ms = now;

    if (s_stall_stopped) {
        // odometry タスクが停止させた。PWM の記憶を 0 にして次の計算で必ず書き直す。
        ++s_stall_count;
        Serial.printf("[Drive] loop stall %lu ms -> wheels stopped by guard (count=%lu)\n",
                      static_cast<unsigned long>(s_stall_ms),
                      static_cast<unsigned long>(s_stall_count));
        s_pwm_l = 0;
        s_pwm_r = 0;
        s_stall_stopped = false;
    }

    float v = 0.0f, w = 0.0f;
    DriveSource src = DRIVE_SRC_NONE;
    bool raw_pwm = false;  // 生 PWM (wheel_pwm) を適用中か

    if ((s_source == DRIVE_SRC_HUB || s_source == DRIVE_SRC_AUTO) && s_hub_valid) {
        if (now - s_hub_ms > DRIVE_TIMEOUT_MS) {
            // drive と wheel_pwm でウォッチドッグは共通 (どちらも s_hub_ms を更新する)。
            s_hub_valid = false;
            s_hub_pwm_active = false;
            if (!s_timeout_logged) {
                Serial.println("[Drive] timeout -> stop");
                s_timeout_logged = true;
                s_timeout_pending = true;
            }
        } else if (s_hub_pwm_active) {
            // 校正中: 写像を通さず生 PWM を書く。cmd.v/w は 0、表示ソースは calib。
            src = DRIVE_SRC_CALIB;
            raw_pwm = true;
        } else {
            v = s_hub_v;
            w = s_hub_w;
            src = s_hub_src;
        }
    } else if (s_source == DRIVE_SRC_JOYSTICK && s_local_valid) {
        // ローカル入力にも同じウォッチドッグをかけるが、event は上げず黙って止める。
        if (now - s_local_ms > DRIVE_TIMEOUT_MS) {
            s_local_valid = false;
        } else {
            v = s_local_v;
            w = s_local_w;
            src = DRIVE_SRC_JOYSTICK;
        }
    }

    ddm::clamp_cmd(&v, &w, MAX_V_MPS, MAX_W_RADPS);
    if (src == DRIVE_SRC_NONE) {
        v = 0.0f;
        w = 0.0f;
    }

    const DriveSource prev_src = s_applied_src;
    s_applied_v = v;
    s_applied_w = w;
    s_applied_src = src;

    int32_t pwm_l = 0, pwm_r = 0;
    if (raw_pwm) {
        // 生 PWM は受理時にクランプ済み。逆運動学・写像・P 補正は通さない。
        pwm_l = s_hub_pwm_l;
        pwm_r = s_hub_pwm_r;
    } else {
        // 逆運動学 → (任意の P 補正) → 左右別 PWM 写像
        ddm::Geometry geom = ddm::make_geometry(WHEEL_DIAMETER_MM, TRACK_WIDTH_MM);
        float wl = 0.0f, wr = 0.0f;
        ddm::inverse_kinematics(geom, v, w, &wl, &wr);

        float meas_l = 0.0f, meas_r = 0.0f;
        bool meas_l_valid = false, meas_r_valid = false;
        odometry_get_wheel_omega(&meas_l, &meas_l_valid, &meas_r, &meas_r_valid);

        ddm::wheel_omegas_to_pwm(map_left(), map_right(), wl, wr, WHEEL_KP, meas_l_valid, meas_l,
                                 meas_r_valid, meas_r, &pwm_l, &pwm_r);
    }

    // 以降の書き込み間隔・停止処理は通常経路と生 PWM 経路で共通。
    const int16_t pl = static_cast<int16_t>(pwm_l);
    const int16_t pr = static_cast<int16_t>(pwm_r);

    if (src == DRIVE_SRC_NONE && prev_src != DRIVE_SRC_NONE) {
        // 停止は最短間隔を無視して即座に書く。
        stop_now();
        return;
    }

    const bool changed = (pl != s_pwm_l || pr != s_pwm_r);
    const uint32_t since = now - s_last_write_ms;
    if ((changed && since >= DRIVE_WRITE_MIN_MS) || since >= DRIVE_WRITE_KEEPALIVE_MS) {
        wheel_driver_set_pwm(pl, pr);
        publish_cmd_omega(pl, pr);
        s_pwm_l = pl;
        s_pwm_r = pr;
        s_last_write_ms = now;
    }
}

void drive_controller_stall_guard(uint32_t now_ms) {
    if (s_stall_stopped) return;
    const uint32_t last = s_last_update_ms;
    if (last == 0) return;  // update() 未呼び出し (起動直後)
    if (now_ms - last <= DRIVE_TIMEOUT_MS) return;
    if (s_pwm_l == 0 && s_pwm_r == 0) return;
    s_stall_ms = now_ms - last;
    s_stall_stopped = true;
    wheel_driver_stop();
    publish_cmd_omega(0, 0);
}

void drive_controller_get_state(DriveState* out) {
    if (!out) return;
    out->v = s_applied_v;
    out->w = s_applied_w;
    out->src = s_applied_src;
    out->source = s_source;
    out->pwm_l = s_pwm_l;
    out->pwm_r = s_pwm_r;
    out->last_seq = s_last_seq;
}

bool drive_controller_timeout_event_pending() {
    return s_timeout_pending;
}

void drive_controller_clear_timeout_event() {
    s_timeout_pending = false;
}

const char* drive_source_str(DriveSource s) {
    switch (s) {
        case DRIVE_SRC_HUB:
            return "hub";
        case DRIVE_SRC_JOYSTICK:
            return "joystick";
        case DRIVE_SRC_AUTO:
            return "auto";
        case DRIVE_SRC_CALIB:
            return "calib";
        default:
            return "none";
    }
}
