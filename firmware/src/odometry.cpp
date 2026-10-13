#include "odometry.h"

#include <Arduino.h>
#include <freertos/FreeRTOS.h>
#include <freertos/semphr.h>
#include <freertos/task.h>

#include "config.h"
#include "diffdrive_math.h"
#include "drive_controller.h"
#include "wheel_driver.h"

// ---------------------------------------------------------------------------
// 状態。速度窓のサンプル数は ODOM_VEL_WINDOW_MS / ODOM_SAMPLE_MS。
// ---------------------------------------------------------------------------
static constexpr int kVelWindow = ODOM_VEL_WINDOW_MS / ODOM_SAMPLE_MS;
static constexpr int kSpeedEvery = 10;  // ReadSpeed は 10 サンプルに 1 回 (診断用)

static ddm::Geometry s_geom = ddm::make_geometry(WHEEL_DIAMETER_MM, TRACK_WIDTH_MM);
static ddm::PosTracker s_track_l;
static ddm::PosTracker s_track_r;
static ddm::Pose s_pose;
static ddm::VelocityWindow<kVelWindow> s_vel;

static float s_acc_dl = 0.0f;  // 前回 take_snapshot からの累積 [m]
static float s_acc_dr = 0.0f;
static int s_l_pos = -1;
static int s_r_pos = -1;
static bool s_l_valid = false;
static bool s_r_valid = false;
static float s_cmd_wl = 0.0f;  // 指令車輪角速度 (車体前進で正)
static float s_cmd_wr = 0.0f;
static int s_l_spd = 0;
static int s_r_spd = 0;
static uint32_t s_samples = 0;
static uint32_t s_read_fail = 0;
static uint32_t s_sample_ms = 0;  // 直近 ReadPos の millis()

static SemaphoreHandle_t s_mutex = nullptr;

static inline void odom_lock() {
    if (s_mutex) xSemaphoreTake(s_mutex, portMAX_DELAY);
}

static inline void odom_unlock() {
    if (s_mutex) xSemaphoreGive(s_mutex);
}

// ---------------------------------------------------------------------------
// サンプリングタスク (50 Hz)
// ---------------------------------------------------------------------------
static void odometry_task(void* arg) {
    (void)arg;
    ddm::PosTrackerConfig cfg = ddm::make_pos_tracker_config(
        WHEEL_POS_STEPS, WHEEL_POS_VALID_DEG, WHEEL_POS_VALID_MIN, WHEEL_POS_VALID_MAX,
        WHEEL_OMEGA_PHYS_MAX_RADPS, WHEEL_DEAD_DEG, WHEEL_RELOCK_TOL_STEP, WHEEL_RELOCK_SAMPLES,
        WHEEL_LOST_DEG, WHEEL_OMEGA_FRESH_MS * 1e-3f, WHEEL_OMEGA_EMA_ALPHA);
    odom_lock();
    s_track_l.configure(cfg);
    s_track_r.configure(cfg);
    odom_unlock();

    const TickType_t period = pdMS_TO_TICKS(ODOM_SAMPLE_MS);
    TickType_t last_wake = xTaskGetTickCount();
    uint32_t last_us = micros();
    uint32_t tick = 0;

    for (;;) {
        vTaskDelayUntil(&last_wake, period);

        uint32_t now_us = micros();
        float dt = static_cast<float>(now_us - last_us) * 1e-6f;  // 桁溢れしても差分は正しい
        last_us = now_us;
        if (dt <= 0.0f) dt = ODOM_SAMPLE_MS * 1e-3f;

        // wheel_driver は自前のミューテックスを持つ。オドメトリのミューテックスは保持しない。
        int lp = -1, rp = -1;
        bool ok = wheel_driver_read_pos(&lp, &rp);
        const uint32_t sample_ms = millis();  // 姿勢の時刻 (odom の ts に載せる)

        bool have_speed = false;
        int ls = 0, rs = 0;
        if ((tick % kSpeedEvery) == 0) have_speed = wheel_driver_read_speed(&ls, &rs);
        ++tick;

        // サーボの取り付け符号と ReadPos の計数方向 (WHEEL_POS_SIGN: PWM 正方向と逆)。
        // トラッカには指令 ω を計数方向に直して渡し、Δθ には符号を戻して dl/dr を
        // 車体前進で正にする。
        constexpr float kLeftSign = static_cast<float>(WHEEL_LEFT_SIGN * WHEEL_POS_SIGN);
        constexpr float kRightSign = static_cast<float>(WHEEL_RIGHT_SIGN * WHEEL_POS_SIGN);
        odom_lock();
        float dth_l = s_track_l.update(lp, dt, s_cmd_wl * kLeftSign);
        float dth_r = s_track_r.update(rp, dt, s_cmd_wr * kRightSign);
        float dl = s_geom.wheel_radius_m * dth_l * kLeftSign;
        float dr = s_geom.wheel_radius_m * dth_r * kRightSign;

        float ds = 0.0f, dth = 0.0f;
        ddm::integrate_pose(s_geom, &s_pose, dl, dr, &ds, &dth);
        s_vel.push(ds, dth, dt);
        s_acc_dl += dl;
        s_acc_dr += dr;

        s_l_pos = lp;
        s_r_pos = rp;
        s_l_valid = s_track_l.last_valid();
        s_r_valid = s_track_r.last_valid();
        if (have_speed) {
            s_l_spd = ls;
            s_r_spd = rs;
        }
        ++s_samples;
        s_sample_ms = sample_ms;
        if (!ok) ++s_read_fail;
        odom_unlock();

        // loop() が止まっていても最後の PWM で走り続けないようにする (ミューテックス外で呼ぶ)。
        drive_controller_stall_guard(millis());
    }
}

// ---------------------------------------------------------------------------
// API
// ---------------------------------------------------------------------------
void odometry_init() {
    if (s_mutex == nullptr) s_mutex = xSemaphoreCreateMutex();
    xTaskCreatePinnedToCore(odometry_task, "odom", ODOM_TASK_STACK, nullptr, ODOM_TASK_PRIO,
                            nullptr, ODOM_TASK_CORE);
    Serial.printf("[Odom] task started (%d Hz, window=%d samples)\n", 1000 / ODOM_SAMPLE_MS,
                  kVelWindow);
}

static void fill_snapshot(OdomSnapshot* out) {
    out->x = s_pose.x;
    out->y = s_pose.y;
    out->th = s_pose.th;
    float v = 0.0f, w = 0.0f;
    s_vel.get(&v, &w);
    out->v = v;
    out->w = w;
    out->dl = s_acc_dl;
    out->dr = s_acc_dr;
    out->l_pos = s_l_pos;
    out->r_pos = s_r_pos;
    out->l_valid = s_l_valid;
    out->r_valid = s_r_valid;
    out->l_state = static_cast<uint8_t>(s_track_l.state());
    out->r_state = static_cast<uint8_t>(s_track_r.state());
    out->l_src = static_cast<uint8_t>(s_track_l.src());
    out->r_src = static_cast<uint8_t>(s_track_r.src());
    const ddm::PosTrackerStats& sl = s_track_l.stats();
    const ddm::PosTrackerStats& sr = s_track_r.stats();
    out->l_cross = sl.cross;
    out->l_lost = sl.lost;
    out->l_reject = sl.reject;
    out->l_extrap_rad = static_cast<float>(sl.extrap_rad);
    out->r_cross = sr.cross;
    out->r_lost = sr.lost;
    out->r_reject = sr.reject;
    out->r_extrap_rad = static_cast<float>(sr.extrap_rad);
    out->l_spd = s_l_spd;
    out->r_spd = s_r_spd;
    out->samples = s_samples;
    out->read_fail = s_read_fail;
    out->sample_ms = s_sample_ms;
}

void odometry_take_snapshot(OdomSnapshot* out) {
    if (!out) return;
    odom_lock();
    fill_snapshot(out);
    s_acc_dl = 0.0f;
    s_acc_dr = 0.0f;
    odom_unlock();
}

void odometry_peek(OdomSnapshot* out) {
    if (!out) return;
    odom_lock();
    fill_snapshot(out);
    odom_unlock();
}

void odometry_get_wheel_omega(float* wl, bool* l_valid, float* wr, bool* r_valid) {
    odom_lock();
    // 指令側 (wl, wr は車体前進で正) と同じ符号系に揃える (KP 補正の比較用)。
    if (wl) *wl = s_track_l.omega() * static_cast<float>(WHEEL_LEFT_SIGN * WHEEL_POS_SIGN);
    if (l_valid) *l_valid = s_track_l.last_valid() && s_track_l.has_omega();
    if (wr) *wr = s_track_r.omega() * static_cast<float>(WHEEL_RIGHT_SIGN * WHEEL_POS_SIGN);
    if (r_valid) *r_valid = s_track_r.last_valid() && s_track_r.has_omega();
    odom_unlock();
}

void odometry_reset() {
    odom_lock();
    // 車輪位置は連続しているのでトラッカー (状態・統計) はリセットしない。
    s_pose = ddm::Pose{};
    s_vel.reset();
    s_acc_dl = 0.0f;
    s_acc_dr = 0.0f;
    odom_unlock();
}

void odometry_set_cmd_omega(float wl, float wr) {
    odom_lock();
    s_cmd_wl = wl;
    s_cmd_wr = wr;
    odom_unlock();
}
