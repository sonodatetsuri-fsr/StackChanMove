#pragma once
// ---------------------------------------------------------------------------
// odometry — 専用 FreeRTOS タスク (ODOM_TASK_CORE / ODOM_TASK_PRIO / ODOM_SAMPLE_MS) で
// 両サーボの ReadPos を読み、diffdrive_math の PosTracker + integrate_pose で
// (x, y, θ) を積算する。積算値はミューテックスで保護する。
// ---------------------------------------------------------------------------

#include <cstdint>

struct OdomSnapshot {
    float x;    // [m]
    float y;    // [m]
    float th;   // [rad] −π..π
    float v;    // [m/s]  直近 ODOM_VEL_WINDOW_MS の移動平均
    float w;    // [rad/s]
    float dl;   // 前回 odometry_take_snapshot() からの左車輪移動量 [m] (車体前進正)
    float dr;   // 同 右
    int l_pos;  // 直近の ReadPos (失敗時 −1)
    int r_pos;
    bool l_valid;  // トラッカが locked (読み値の差分を積算中) か
    bool r_valid;
    uint8_t l_state;  // ddm::PosState (0 locked / 1 crossing / 2 lost)
    uint8_t r_state;
    uint8_t l_src;  // ddm::PosSrc: 直近サンプルの Δθ の出所 (0 none / 1 meas / 2 cmd)
    uint8_t r_src;
    // トラッカ統計 (odom_reset でクリアしない累積)
    uint32_t l_cross, l_lost, l_reject;
    uint32_t r_cross, r_lost, r_reject;
    float l_extrap_rad;
    float r_extrap_rad;
    int l_spd;  // 直近の ReadSpeed (診断用、更新はサンプル 10 回に 1 回程度でよい)
    int r_spd;
    uint32_t samples;    // 起動からの総サンプル数 (診断用)
    uint32_t read_fail;  // ReadPos 失敗回数 (診断用)
    uint32_t sample_ms;  // 直近サンプルの ReadPos 時刻 millis() (odom の ts に使う)
};

/// ミューテックス生成とタスク起動。wheel_driver_init() の後に呼ぶ。
void odometry_init();

/// 積算値と、前回呼び出しからの累積 dl/dr を取り出す (dl/dr はこの呼び出しで 0 に戻す)。
/// odom 送信 (10 Hz) 用。
void odometry_take_snapshot(OdomSnapshot* out);

/// 累積 dl/dr をリセットせずに現在値を読む (診断ログ用)。
void odometry_peek(OdomSnapshot* out);

/// 実測車輪角速度 [rad/s] (車体前進正、符号適用済み) と有効フラグ。drive_controller の
/// WHEEL_KP 補正用。位置無効区間中は valid=false (補正を凍結する)。
void odometry_get_wheel_omega(float* wl, bool* l_valid, float* wr, bool* r_valid);

/// x = y = θ = 0 (odom_reset)。dl/dr 累積と速度窓もクリアする。
void odometry_reset();

/// drive_controller が PWM を書くたびに渡す指令車輪角速度 [rad/s] (車体前進で正)。
/// 適用中の PWM から ddm::pwm_to_wheel_omega で逆算した値。停止書き込みでは (0, 0)。
/// 不感帯横断中に実測 ω が古くなったときの外挿に使う。
void odometry_set_cmd_omega(float wl, float wr);
