// ---------------------------------------------------------------------------
// diffdrive_math.h の単体テスト。
// ホスト実行: `pio test -e native` (firmware/ で実行。テストベクタは ../tests/fixtures)
// ---------------------------------------------------------------------------
#include <ArduinoJson.h>
#include <unity.h>

#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <string>

#include "diffdrive_math.h"

using namespace ddm;

static constexpr float kEps = 1e-5f;
// 1 step あたりの車輪回転角 [rad] (300° / 1024 step)
static constexpr float kStepRad = (300.0f / 1024.0f) * kPi / 180.0f;

void setUp(void) {}
void tearDown(void) {}

// ---------------------------------------------------------------------------
// Geometry
// ---------------------------------------------------------------------------
static void test_make_geometry(void) {
    Geometry g = make_geometry(37.0f, 77.5f);
    TEST_ASSERT_FLOAT_WITHIN(kEps, 0.0185f, g.wheel_radius_m);
    TEST_ASSERT_FLOAT_WITHIN(kEps, 0.0775f, g.track_m);
}

// ---------------------------------------------------------------------------
// Inverse kinematics
// ---------------------------------------------------------------------------
static void test_inverse_kinematics_straight(void) {
    Geometry g = make_geometry(37.0f, 77.5f);
    float wl = 0.0f, wr = 0.0f;
    inverse_kinematics(g, 0.1f, 0.0f, &wl, &wr);
    TEST_ASSERT_FLOAT_WITHIN(1e-4f, 0.1f / 0.0185f, wl);
    TEST_ASSERT_FLOAT_WITHIN(1e-4f, 0.1f / 0.0185f, wr);
    TEST_ASSERT_FLOAT_WITHIN(kEps, wl, wr);
}

static void test_inverse_kinematics_spin(void) {
    Geometry g = make_geometry(37.0f, 77.5f);
    float wl = 0.0f, wr = 0.0f;
    inverse_kinematics(g, 0.0f, 1.0f, &wl, &wr);
    // 超信地旋回: 左右が逆符号で同じ大きさ。wr = (T/2)/r
    TEST_ASSERT_FLOAT_WITHIN(1e-4f, (0.0775f / 2.0f) / 0.0185f, wr);
    TEST_ASSERT_FLOAT_WITHIN(1e-4f, -wr, wl);
}

// ---------------------------------------------------------------------------
// clamp_cmd
// ---------------------------------------------------------------------------
static void test_clamp_cmd(void) {
    float v = 1.0f, w = 5.0f;
    clamp_cmd(&v, &w, 0.15f, 2.0f);
    TEST_ASSERT_FLOAT_WITHIN(kEps, 0.15f, v);
    TEST_ASSERT_FLOAT_WITHIN(kEps, 2.0f, w);

    v = -1.0f;
    w = -5.0f;
    clamp_cmd(&v, &w, 0.15f, 2.0f);
    TEST_ASSERT_FLOAT_WITHIN(kEps, -0.15f, v);
    TEST_ASSERT_FLOAT_WITHIN(kEps, -2.0f, w);

    v = 0.05f;
    w = -1.0f;
    clamp_cmd(&v, &w, 0.15f, 2.0f);
    TEST_ASSERT_FLOAT_WITHIN(kEps, 0.05f, v);
    TEST_ASSERT_FLOAT_WITHIN(kEps, -1.0f, w);
}

// ---------------------------------------------------------------------------
// PWM mapping
// ---------------------------------------------------------------------------
static PwmMap make_map(void) {
    return PwmMap{60, 95.0f, 1000, 0.05f};
}

static void test_omega_to_pwm(void) {
    PwmMap m = make_map();
    TEST_ASSERT_EQUAL_INT32(0, omega_to_pwm(m, 0.0f));
    TEST_ASSERT_EQUAL_INT32(0, omega_to_pwm(m, 0.04f));
    TEST_ASSERT_EQUAL_INT32(0, omega_to_pwm(m, -0.04f));
    // deadband + per_rad_s * |ω|
    TEST_ASSERT_EQUAL_INT32(155, omega_to_pwm(m, 1.0f));
    TEST_ASSERT_EQUAL_INT32(-155, omega_to_pwm(m, -1.0f));
    TEST_ASSERT_EQUAL_INT32(250, omega_to_pwm(m, 2.0f));
}

static void test_scale_pwm_pair(void) {
    int32_t l = 2000, r = 1000;
    scale_pwm_pair(&l, &r, 1000);
    TEST_ASSERT_EQUAL_INT32(1000, l);
    TEST_ASSERT_EQUAL_INT32(500, r);

    l = -2000;
    r = 500;
    scale_pwm_pair(&l, &r, 1000);
    TEST_ASSERT_EQUAL_INT32(-1000, l);
    TEST_ASSERT_EQUAL_INT32(250, r);

    l = 300;
    r = -400;
    scale_pwm_pair(&l, &r, 1000);
    TEST_ASSERT_EQUAL_INT32(300, l);
    TEST_ASSERT_EQUAL_INT32(-400, r);
}

static void test_clamp_pwm(void) {
    TEST_ASSERT_EQUAL_INT32(1000, clamp_pwm(2000, 1000));
    TEST_ASSERT_EQUAL_INT32(-1000, clamp_pwm(-2000, 1000));
    TEST_ASSERT_EQUAL_INT32(300, clamp_pwm(300, 1000));
    TEST_ASSERT_EQUAL_INT32(-1000, clamp_pwm(-1000, 1000));
}

// 左右対称の写像 (既定値) — 左右別化前と同じ結果になること
static void test_wheel_omegas_to_pwm_open_loop(void) {
    PwmMap m = make_map();
    int32_t pl = 0, pr = 0;
    wheel_omegas_to_pwm(m, m, 1.0f, -2.0f, 0.0f, true, 0.0f, true, 0.0f, &pl, &pr);
    TEST_ASSERT_EQUAL_INT32(omega_to_pwm(m, 1.0f), pl);
    TEST_ASSERT_EQUAL_INT32(omega_to_pwm(m, -2.0f), pr);
    TEST_ASSERT_EQUAL_INT32(155, pl);
    TEST_ASSERT_EQUAL_INT32(-250, pr);
}

static void test_wheel_omegas_to_pwm_p_correction(void) {
    PwmMap m = make_map();
    int32_t pl = 0, pr = 0;
    // kp = 10, 実測が目標より 0.5 rad/s 遅い → +5 の補正
    wheel_omegas_to_pwm(m, m, 1.0f, 1.0f, 10.0f, true, 0.5f, true, 1.0f, &pl, &pr);
    TEST_ASSERT_EQUAL_INT32(155 + 5, pl);
    TEST_ASSERT_EQUAL_INT32(155, pr);

    // 位置無効区間 (meas_valid = false) では補正しない
    wheel_omegas_to_pwm(m, m, 1.0f, 1.0f, 10.0f, false, 0.5f, false, 0.0f, &pl, &pr);
    TEST_ASSERT_EQUAL_INT32(155, pl);
    TEST_ASSERT_EQUAL_INT32(155, pr);
}

// 左右別の写像: 右のデッドバンド / 係数が大きいと同じ ω でも PWM が大きい
static void test_wheel_omegas_to_pwm_asymmetric(void) {
    PwmMap map_l{60, 95.0f, 1000, 0.05f};
    PwmMap map_r{90, 110.0f, 1000, 0.05f};
    int32_t pl = 0, pr = 0;
    wheel_omegas_to_pwm(map_l, map_r, 1.0f, 1.0f, 0.0f, false, 0.0f, false, 0.0f, &pl, &pr);
    TEST_ASSERT_EQUAL_INT32(155, pl);  // 60 + 95
    TEST_ASSERT_EQUAL_INT32(200, pr);  // 90 + 110

    // 負の角速度は符号だけ反転する
    wheel_omegas_to_pwm(map_l, map_r, -1.0f, -1.0f, 0.0f, false, 0.0f, false, 0.0f, &pl, &pr);
    TEST_ASSERT_EQUAL_INT32(-155, pl);
    TEST_ASSERT_EQUAL_INT32(-200, pr);

    // min_omega 未満は左右とも 0
    wheel_omegas_to_pwm(map_l, map_r, 0.01f, 0.01f, 0.0f, false, 0.0f, false, 0.0f, &pl, &pr);
    TEST_ASSERT_EQUAL_INT32(0, pl);
    TEST_ASSERT_EQUAL_INT32(0, pr);
}

// 左右別でも上限縮小は共通の pwm_max で比率を保つ (右が超える場合)
static void test_wheel_omegas_to_pwm_asymmetric_scaled(void) {
    PwmMap map_l{60, 95.0f, 1000, 0.05f};
    PwmMap map_r{90, 110.0f, 1000, 0.05f};
    int32_t pl = 0, pr = 0;
    // 右は 90 + 10·110 = 1190 で上限超え。k = 1000/1190、左 155 → 130
    wheel_omegas_to_pwm(map_l, map_r, 1.0f, 10.0f, 0.0f, false, 0.0f, false, 0.0f, &pl, &pr);
    TEST_ASSERT_EQUAL_INT32(1000, pr);
    TEST_ASSERT_EQUAL_INT32(130, pl);
}

// ---------------------------------------------------------------------------
// PWM 逆写像
// ---------------------------------------------------------------------------
static void test_pwm_to_wheel_omega(void) {
    PwmMap m = make_map();  // deadband 60, 95 PWM/(rad/s)
    TEST_ASSERT_FLOAT_WITHIN(kEps, 0.0f, pwm_to_wheel_omega(0, m));
    TEST_ASSERT_FLOAT_WITHIN(kEps, 0.0f, pwm_to_wheel_omega(60, m));  // デッドバンド内
    TEST_ASSERT_FLOAT_WITHIN(kEps, 0.0f, pwm_to_wheel_omega(-30, m));
    TEST_ASSERT_FLOAT_WITHIN(1e-5f, 1.0f, pwm_to_wheel_omega(155, m));
    TEST_ASSERT_FLOAT_WITHIN(1e-5f, -2.0f, pwm_to_wheel_omega(-250, m));
    // omega_to_pwm との往復 (丸め 0.5 PWM = 0.0053 rad/s 以内)
    for (float w = -8.0f; w <= 8.0f; w += 0.37f) {
        int32_t p = omega_to_pwm(m, w);
        float back = pwm_to_wheel_omega(p, m);
        if (std::fabs(w) < m.min_omega)
            TEST_ASSERT_FLOAT_WITHIN(kEps, 0.0f, back);
        else
            TEST_ASSERT_FLOAT_WITHIN(0.5f / m.per_rad_s + 1e-5f, w, back);
    }
    PwmMap zero{60, 0.0f, 1000, 0.05f};
    TEST_ASSERT_FLOAT_WITHIN(kEps, 0.0f, pwm_to_wheel_omega(500, zero));
}

// ---------------------------------------------------------------------------
// PosTracker v2
// ---------------------------------------------------------------------------
static PosTrackerConfig default_cfg(void) {
    // config.h.example の既定値 (valid 12..1012、物理上限 12 rad/s、不感帯 40°、
    // 再ロック ±60 step × 2 サンプル、lost 120°、実測 ω 期限 200 ms、EMA 0.3)
    return make_pos_tracker_config(1024, 300.0f, 12, 1012);
}

static constexpr float kDt = 0.02f;
// 10 step/サンプル (≈ 2.56 rad/s) に相当する指令 ω
static const float kW10 = 10.0f * kStepRad / kDt;
static constexpr float kDeadSteps = 1024.0f * 40.0f / 300.0f;  // 不感帯 40° の step 換算

/// 読み値列を流し、Δθ の合計を返す。
static float run_seq(PosTracker& t, const int* pos, int n, float omega_cmd, float dt = kDt) {
    float sum = 0.0f;
    for (int i = 0; i < n; ++i) sum += t.update(pos[i], dt, omega_cmd);
    return sum;
}

static void test_pos_tracker_first_sample(void) {
    PosTracker t(default_cfg());
    TEST_ASSERT_EQUAL(PosState::Lost, t.state());
    TEST_ASSERT_FLOAT_WITHIN(kEps, 0.0f, t.update(500, kDt, 0.0f));
    TEST_ASSERT_EQUAL(PosState::Locked, t.state());
    TEST_ASSERT_TRUE(t.last_valid());
    TEST_ASSERT_FALSE(t.has_omega());
    TEST_ASSERT_EQUAL(PosSrc::None, t.src());
}

static void test_pos_tracker_first_sample_on_rail_waits(void) {
    // 起動時に飽和端 (不感帯内) なら最初の範囲内読み値までロックしない
    PosTracker t(default_cfg());
    TEST_ASSERT_FLOAT_WITHIN(kEps, 0.0f, t.update(1022, kDt, 0.0f));
    TEST_ASSERT_EQUAL(PosState::Lost, t.state());
    TEST_ASSERT_FALSE(t.last_valid());
    // 指令があれば指令だけで積算する (デッドレコニング)
    TEST_ASSERT_FLOAT_WITHIN(1e-6f, kW10 * kDt, t.update(1022, kDt, kW10));
    TEST_ASSERT_EQUAL(PosSrc::Cmd, t.src());
    t.update(30, kDt, kW10);
    TEST_ASSERT_EQUAL(PosState::Locked, t.state());
}

static void test_pos_tracker_forward(void) {
    PosTracker t(default_cfg());
    t.update(500, kDt, 0.0f);
    float d = t.update(510, kDt, 0.0f);
    TEST_ASSERT_FLOAT_WITHIN(1e-6f, 10.0f * kStepRad, d);
    TEST_ASSERT_TRUE(t.has_omega());
    TEST_ASSERT_FLOAT_WITHIN(1e-4f, 10.0f * kStepRad / kDt, t.omega());
    TEST_ASSERT_EQUAL(PosSrc::Meas, t.src());
    // 実測 ω は指数平滑 (α = 0.3): 20 step/サンプルに変わっても一気には追従しない
    t.update(530, kDt, 0.0f);
    TEST_ASSERT_FLOAT_WITHIN(1e-4f, 13.0f * kStepRad / kDt, t.omega());
    TEST_ASSERT_EQUAL_UINT32(0, t.stats().cross);
    TEST_ASSERT_EQUAL_UINT32(0, t.stats().reject);
}

static void test_pos_tracker_backward(void) {
    PosTracker t(default_cfg());
    t.update(500, kDt, 0.0f);
    TEST_ASSERT_FLOAT_WITHIN(1e-6f, -7.0f * kStepRad, t.update(493, kDt, 0.0f));
    TEST_ASSERT_TRUE(t.omega() < 0.0f);
}

static void test_pos_tracker_reject_physical_limit(void) {
    // 12 rad/s × 20 ms = 0.24 rad = 47 step。それを超える差分は採用せず外挿する。
    PosTracker t(default_cfg());
    t.update(500, kDt, 0.0f);
    t.update(510, kDt, 0.0f);
    float d = t.update(800, kDt, kW10);  // Δ = 290 step (旧トラッカは 1.48 rad を積算していた)
    TEST_ASSERT_FLOAT_WITHIN(1e-6f, kW10 * kDt, d);
    TEST_ASSERT_EQUAL(PosState::Crossing, t.state());
    TEST_ASSERT_FALSE(t.last_valid());
    TEST_ASSERT_EQUAL_UINT32(1, t.stats().reject);
    TEST_ASSERT_EQUAL_UINT32(0, t.stats().cross);
    // 47 step ちょうどは採用する
    PosTracker t2(default_cfg());
    t2.update(500, kDt, 0.0f);
    TEST_ASSERT_FLOAT_WITHIN(1e-6f, 46.0f * kStepRad, t2.update(546, kDt, 0.0f));
    TEST_ASSERT_EQUAL(PosState::Locked, t2.state());
}

static void test_pos_tracker_rail_enters_crossing(void) {
    PosTracker t(default_cfg());
    t.update(1000, kDt, kW10);
    t.update(1010, kDt, kW10);            // 1010 < 1012 なので範囲内
    float d = t.update(1015, kDt, kW10);  // 飽和端
    TEST_ASSERT_EQUAL(PosState::Crossing, t.state());
    TEST_ASSERT_FLOAT_WITHIN(1e-6f, kW10 * kDt, d);  // 指令 ω で外挿
    TEST_ASSERT_EQUAL(PosSrc::Cmd, t.src());
    TEST_ASSERT_EQUAL_UINT32(1, t.stats().cross);
    // 下端も同じ
    PosTracker t2(default_cfg());
    t2.update(20, kDt, -kW10);
    t2.update(12, kDt, -kW10);  // 12 ≤ valid_min → 飽和端
    TEST_ASSERT_EQUAL(PosState::Crossing, t2.state());
    TEST_ASSERT_EQUAL_UINT32(1, t2.stats().cross);
    // 読み取り失敗 (−1) も外挿するが cross には数えない
    PosTracker t3(default_cfg());
    t3.update(500, kDt, kW10);
    t3.update(510, kDt, kW10);
    TEST_ASSERT_FLOAT_WITHIN(1e-6f, kW10 * kDt, t3.update(-1, kDt, kW10));
    TEST_ASSERT_EQUAL(PosState::Crossing, t3.state());
    TEST_ASSERT_EQUAL_UINT32(0, t3.stats().cross);
}

static void test_pos_tracker_extrapolates_with_measured_when_cmd_zero(void) {
    // 停止指令中 (ω_cmd = 0) の横断は直近の実測 ω を WHEEL_OMEGA_FRESH_MS (40 ms) で 0 へ
    // 線形減衰させて外挿する: 20 ms 後 = 半分、40 ms 後 = 0。
    PosTracker t(default_cfg());
    t.update(500, kDt, 0.0f);
    for (int i = 1; i <= 5; ++i) t.update(500 + 10 * i, kDt, 0.0f);  // ω_meas ≈ 10 step/サンプル
    float d = t.update(-1, kDt, 0.0f);
    TEST_ASSERT_FLOAT_WITHIN(1e-4f, 0.5f * t.omega() * kDt, d);
    TEST_ASSERT_EQUAL(PosSrc::Meas, t.src());
    TEST_ASSERT_FLOAT_WITHIN(kEps, 0.0f, t.update(-1, kDt, 0.0f));  // 40 ms: 減衰完了
    TEST_ASSERT_EQUAL(PosSrc::None, t.src());
    TEST_ASSERT_FLOAT_WITHIN(kEps, 0.0f, t.update(-1, kDt, 0.0f));
    // 指令 ω があるときは実測 ω より指令を優先する (停止からの発進で予測が遅れないため)
    PosTracker t2(default_cfg());
    t2.update(500, kDt, 0.0f);
    t2.update(500, kDt, 0.0f);  // ω_meas = 0 (停止中)
    TEST_ASSERT_FLOAT_WITHIN(1e-6f, kW10 * kDt, t2.update(-1, kDt, kW10));
    TEST_ASSERT_EQUAL(PosSrc::Cmd, t2.src());
}

// 右車輪 (ID1) の実測列 (data/sessions/20260901_170259、v ≈ 0.04 m/s、
// 19 step/サンプル): 上端飽和 → 中間値 ≈ 620〜640 → 0 → 通常計数。
static void test_pos_tracker_real_right_sequence(void) {
    static const int seq[] = {994, 1014, 1022, 700, 637, 626, 624, 29, 9, 40, 68, 88, 107, 126};
    const int n = sizeof(seq) / sizeof(seq[0]);
    const float w = 19.0f * kStepRad / kDt;
    PosTracker t(default_cfg());
    float sum = run_seq(t, seq, n, w);
    TEST_ASSERT_EQUAL(PosState::Locked, t.state());
    TEST_ASSERT_EQUAL_UINT32(1, t.stats().cross);
    TEST_ASSERT_EQUAL_UINT32(0, t.stats().lost);
    TEST_ASSERT_EQUAL_UINT32(0, t.stats().reject);
    // 旧トラッカは 1022→767 や 623→0 (折り返し +401) を積算して 1 回転あたり −95° の
    // 偽差分を出していた。新トラッカは横断の合計角が幾何どおり (994 → 1024 → 不感帯 40°
    // → 0 → 126) になる (再ロック時に予測との残差を補正する)。
    float expect = ((1024.0f - 994.0f) + kDeadSteps + 126.0f) * kStepRad;
    TEST_ASSERT_TRUE(sum > 0.0f);
    TEST_ASSERT_FLOAT_WITHIN(1.0f * kStepRad, expect, sum);
}

// 左車輪 (ID2) の実測列: 読み値減少方向で不感帯に入り、下端で這うように 0→9、ゴミ 1 個、
// 上端飽和、通常計数に戻る。
static void test_pos_tracker_real_left_sequence(void) {
    static const int seq[] = {15, 6,   0,    0,    0,    0,    2,   3,   4,   5,  7,
                              7,  974, 1022, 1019, 1009, 1000, 990, 980, 970, 960};
    const int n = sizeof(seq) / sizeof(seq[0]);
    const float w = -9.0f * kStepRad / kDt;
    PosTracker t(default_cfg());
    float sum = run_seq(t, seq, n, w);
    TEST_ASSERT_EQUAL(PosState::Locked, t.state());
    TEST_ASSERT_EQUAL_UINT32(1, t.stats().cross);
    TEST_ASSERT_EQUAL_UINT32(0, t.stats().lost);
    float expect = -(15.0f + kDeadSteps + (1024.0f - 960.0f)) * kStepRad;
    TEST_ASSERT_TRUE(sum < 0.0f);
    TEST_ASSERT_FLOAT_WITHIN(1.0f * kStepRad, expect, sum);
}

static void test_pos_tracker_stop_in_deadzone_and_restart(void) {
    // 10 step/サンプルで 1000 から進み、1024 + 不感帯 136.5 step の後に読み値 0 へ出る
    // (= 停止をはさんで合計 16 サンプル)。
    PosTracker t(default_cfg());
    for (int i = 0; i <= 10; ++i) t.update(900 + 10 * i, kDt, kW10);  // 900..1000 locked
    t.update(1015, kDt, kW10);
    t.update(1022, kDt, kW10);
    for (int i = 0; i < 4; ++i) t.update(630, kDt, kW10);  // 中間値 (まだ回っている)
    TEST_ASSERT_EQUAL(PosState::Crossing, t.state());
    // 停止: 指令 0 → 実測 ω を 40 ms で減衰 → 外挿 0 で crossing を保持
    float held = 0.0f;
    for (int i = 0; i < 30; ++i) held += std::fabs(t.update(630, kDt, 0.0f));
    TEST_ASSERT_EQUAL(PosState::Crossing, t.state());
    TEST_ASSERT_EQUAL(PosSrc::None, t.src());
    TEST_ASSERT_TRUE(held <= 10.0f * kStepRad);  // 減衰分 (≈ 0.5 サンプル分の実測 ω) だけ
    // 発進: 指令 ω で予測が動き出す。中間値 630 は位置不整合で再ロックされず、
    // 出口 (0 → 14 → 24) で再ロックする。
    for (int i = 0; i < 10; ++i) {
        TEST_ASSERT_FLOAT_WITHIN(1e-6f, kW10 * kDt, t.update(630, kDt, kW10));
        TEST_ASSERT_EQUAL(PosState::Crossing, t.state());
    }
    t.update(0, kDt, kW10);
    t.update(14, kDt, kW10);
    TEST_ASSERT_EQUAL(PosState::Crossing, t.state());
    t.update(24, kDt, kW10);
    TEST_ASSERT_EQUAL(PosState::Locked, t.state());
    TEST_ASSERT_EQUAL_UINT32(0, t.stats().lost);
    TEST_ASSERT_TRUE(std::fabs(t.last_relock_step()) <= 60.0);
    TEST_ASSERT_FLOAT_WITHIN(1e-6f, 10.0f * kStepRad, t.update(34, kDt, kW10));
}

static void test_pos_tracker_lost_and_recovery(void) {
    PosTracker t(default_cfg());
    t.update(500, kDt, kW10);
    t.update(510, kDt, kW10);
    // 整合しない読み値が続く: 120° (= 410 step、41 サンプル) の外挿で lost
    int n = 0;
    while (t.state() != PosState::Lost && n < 200) {
        t.update((n % 2) ? 900 : 300, kDt, kW10);
        ++n;
    }
    TEST_ASSERT_EQUAL(PosState::Lost, t.state());
    TEST_ASSERT_EQUAL_UINT32(1, t.stats().lost);
    TEST_ASSERT_TRUE(n >= 41 && n <= 43);
    // lost 中は指令 ω だけで積算
    TEST_ASSERT_FLOAT_WITHIN(1e-6f, kW10 * kDt, t.update(300, kDt, kW10));
    TEST_ASSERT_EQUAL(PosSrc::Cmd, t.src());
    TEST_ASSERT_FLOAT_WITHIN(kEps, 0.0f, t.update(300, kDt, 0.0f));
    TEST_ASSERT_EQUAL(PosSrc::None, t.src());
    // 物理上限内の差分が 3 回続けば再ロック (絶対位置は問わない)。飛びで数え直し。
    t.update(700, kDt, kW10);  // 300 → 700 は物理上限超え: 候補数リセット
    t.update(710, kDt, kW10);
    t.update(720, kDt, kW10);
    TEST_ASSERT_EQUAL(PosState::Lost, t.state());
    t.update(730, kDt, kW10);
    TEST_ASSERT_EQUAL(PosState::Locked, t.state());
    TEST_ASSERT_FLOAT_WITHIN(1e-6f, 10.0f * kStepRad, t.update(740, kDt, kW10));
}

static void test_pos_tracker_read_failure_relocks_with_correction(void) {
    // 範囲内での読み取り失敗 2 回: 外挿の誤差は再ロック時の残差補正で消える
    PosTracker t(default_cfg());
    t.update(500, kDt, kW10);
    float sum = 0.0f;
    static const int seq[] = {510, -1, -1, 540, 550, 560};
    sum += run_seq(t, seq, 6, kW10 * 0.7f);  // 指令は実際より 30% 遅い
    TEST_ASSERT_EQUAL(PosState::Locked, t.state());
    TEST_ASSERT_FLOAT_WITHIN(1e-5f, 60.0f * kStepRad, sum);
    TEST_ASSERT_EQUAL_UINT32(0, t.stats().cross);
    TEST_ASSERT_EQUAL_UINT32(0, t.stats().lost);
}

static void test_pos_tracker_stats_and_reset(void) {
    PosTracker t(default_cfg());
    t.update(1000, kDt, kW10);
    t.update(1015, kDt, kW10);
    t.update(1022, kDt, kW10);
    TEST_ASSERT_EQUAL_UINT32(1, t.stats().cross);
    TEST_ASSERT_TRUE(t.stats().extrap_rad > 0.0);
    t.reset();  // 統計は保持、状態は初期化
    TEST_ASSERT_EQUAL(PosState::Lost, t.state());
    TEST_ASSERT_EQUAL_UINT32(1, t.stats().cross);
    TEST_ASSERT_EQUAL_STRING("locked", pos_state_str(PosState::Locked));
    TEST_ASSERT_EQUAL_STRING("crossing", pos_state_str(PosState::Crossing));
    TEST_ASSERT_EQUAL_STRING("lost", pos_state_str(PosState::Lost));
    TEST_ASSERT_EQUAL('L', pos_state_char(PosState::Locked));
    TEST_ASSERT_EQUAL('C', pos_state_char(PosState::Crossing));
    TEST_ASSERT_EQUAL('X', pos_state_char(PosState::Lost));
    TEST_ASSERT_EQUAL_STRING("meas", pos_src_str(PosSrc::Meas));
    TEST_ASSERT_EQUAL_STRING("cmd", pos_src_str(PosSrc::Cmd));
    TEST_ASSERT_EQUAL_STRING("none", pos_src_str(PosSrc::None));
}

// ---------------------------------------------------------------------------
// テストベクタ: tests/fixtures/pos_tracker_vectors.json を Python 実装
// (movehub.services.pos_tracker) と共有し、C++ と Python の出力一致を要求する。
// ---------------------------------------------------------------------------
static bool read_file(const char* path, std::string* out) {
    FILE* f = fopen(path, "rb");
    if (!f) return false;
    char buf[4096];
    size_t n;
    while ((n = fread(buf, 1, sizeof(buf), f)) > 0) out->append(buf, n);
    fclose(f);
    return true;
}

static PosState state_from_str(const char* s) {
    if (strcmp(s, "locked") == 0) return PosState::Locked;
    if (strcmp(s, "crossing") == 0) return PosState::Crossing;
    return PosState::Lost;
}

static void test_pos_tracker_vectors(void) {
    std::string text;
    const char* env = getenv("POS_TRACKER_VECTORS");
    const char* candidates[] = {env, "../tests/fixtures/pos_tracker_vectors.json",
                                "tests/fixtures/pos_tracker_vectors.json"};
    bool found = false;
    for (const char* c : candidates) {
        if (c && read_file(c, &text)) {
            found = true;
            break;
        }
    }
    if (!found) TEST_FAIL_MESSAGE("tests/fixtures/pos_tracker_vectors.json not found");

    JsonDocument doc;
    DeserializationError err = deserializeJson(doc, text);
    if (err) TEST_FAIL_MESSAGE(err.c_str());

    JsonObject c = doc["config"];
    PosTrackerConfig cfg = make_pos_tracker_config(
        c["steps"].as<int>(), c["valid_deg"].as<float>(), c["valid_min"].as<int>(),
        c["valid_max"].as<int>(), c["omega_max"].as<float>(), c["dead_deg"].as<float>(),
        c["relock_tol_step"].as<int>(), c["relock_samples"].as<int>(), c["lost_deg"].as<float>(),
        c["fresh_s"].as<float>(), c["omega_ema_alpha"].as<float>());

    int cases = 0;
    int samples = 0;
    for (JsonObject cs : doc["cases"].as<JsonArray>()) {
        const char* name = cs["name"];
        JsonArray pos = cs["pos"];
        JsonArray dt = cs["dt"];
        JsonArray wc = cs["omega_cmd"];
        JsonArray dth = cs["dtheta"];
        JsonArray st = cs["state"];
        JsonArray src = cs["src"];
        TEST_ASSERT_EQUAL_MESSAGE(pos.size(), dth.size(), name);
        PosTracker t(cfg);
        for (size_t i = 0; i < pos.size(); ++i) {
            float d = t.update(pos[i].as<int>(), dt[i].as<float>(), wc[i].as<float>());
            char msg[96];
            snprintf(msg, sizeof(msg), "%s[%u] pos=%d", name, static_cast<unsigned>(i),
                     pos[i].as<int>());
            TEST_ASSERT_EQUAL_MESSAGE(state_from_str(st[i].as<const char*>()), t.state(), msg);
            TEST_ASSERT_EQUAL_STRING_MESSAGE(src[i].as<const char*>(), pos_src_str(t.src()), msg);
            // dt / ω_cmd は float で渡すため double の Python と 1e-6 rad 程度まで一致する
            TEST_ASSERT_FLOAT_WITHIN_MESSAGE(2e-6f, dth[i].as<float>(), d, msg);
            ++samples;
        }
        JsonObject stats = cs["stats"];
        TEST_ASSERT_EQUAL_UINT32_MESSAGE(stats["cross"].as<uint32_t>(), t.stats().cross, name);
        TEST_ASSERT_EQUAL_UINT32_MESSAGE(stats["lost"].as<uint32_t>(), t.stats().lost, name);
        TEST_ASSERT_EQUAL_UINT32_MESSAGE(stats["reject"].as<uint32_t>(), t.stats().reject, name);
        TEST_ASSERT_FLOAT_WITHIN_MESSAGE(1e-3f, stats["extrap_rad"].as<float>(),
                                         static_cast<float>(t.stats().extrap_rad), name);
        ++cases;
    }
    TEST_ASSERT_TRUE(cases >= 5);
    TEST_ASSERT_TRUE(samples >= 2000);
}

// ---------------------------------------------------------------------------
// Pose integration
// ---------------------------------------------------------------------------
static void test_integrate_pose_straight(void) {
    Geometry g = make_geometry(37.0f, 77.5f);
    Pose p;
    float ds = 0.0f, dth = 0.0f;
    integrate_pose(g, &p, 0.1f, 0.1f, &ds, &dth);
    TEST_ASSERT_FLOAT_WITHIN(kEps, 0.1f, p.x);
    TEST_ASSERT_FLOAT_WITHIN(kEps, 0.0f, p.y);
    TEST_ASSERT_FLOAT_WITHIN(kEps, 0.0f, p.th);
    TEST_ASSERT_FLOAT_WITHIN(kEps, 0.1f, ds);
    TEST_ASSERT_FLOAT_WITHIN(kEps, 0.0f, dth);
}

static void test_integrate_pose_spin(void) {
    Geometry g = make_geometry(37.0f, 77.5f);
    Pose p;
    const float a = 0.01f;
    integrate_pose(g, &p, -a, a, nullptr, nullptr);
    TEST_ASSERT_FLOAT_WITHIN(1e-5f, 0.0f, p.x);
    TEST_ASSERT_FLOAT_WITHIN(1e-5f, 0.0f, p.y);
    TEST_ASSERT_FLOAT_WITHIN(1e-4f, 2.0f * a / g.track_m, p.th);
}

static void test_integrate_pose_wraps_theta(void) {
    Geometry g = make_geometry(37.0f, 77.5f);
    Pose p;
    p.th = 3.0f;
    const float a = 0.01f;
    integrate_pose(g, &p, -a, a, nullptr, nullptr);
    float expect = wrap_pi(3.0f + 2.0f * a / g.track_m);
    TEST_ASSERT_TRUE(p.th <= kPi && p.th >= -kPi);
    TEST_ASSERT_FLOAT_WITHIN(1e-4f, expect, p.th);
}

static void test_integrate_pose_quarter_circle(void) {
    Geometry g = make_geometry(37.0f, 77.5f);
    Pose p;
    const float R = 0.5f;
    const int n = 2000;
    const float dth = (kPi / 2.0f) / static_cast<float>(n);
    const float ds = R * dth;
    for (int i = 0; i < n; ++i) {
        float dl = ds - dth * g.track_m * 0.5f;
        float dr = ds + dth * g.track_m * 0.5f;
        integrate_pose(g, &p, dl, dr, nullptr, nullptr);
    }
    TEST_ASSERT_FLOAT_WITHIN(1e-3f, kPi / 2.0f, p.th);
    TEST_ASSERT_FLOAT_WITHIN(1e-3f, R, p.x);
    TEST_ASSERT_FLOAT_WITHIN(1e-3f, R, p.y);
}

// ---------------------------------------------------------------------------
// VelocityWindow
// ---------------------------------------------------------------------------
static void test_velocity_window(void) {
    VelocityWindow<10> win;
    float v = -1.0f, w = -1.0f;
    win.get(&v, &w);
    TEST_ASSERT_FLOAT_WITHIN(kEps, 0.0f, v);
    TEST_ASSERT_FLOAT_WITHIN(kEps, 0.0f, w);

    for (int i = 0; i < 10; ++i) win.push(0.002f, 0.01f, 0.02f);
    win.get(&v, &w);
    TEST_ASSERT_FLOAT_WITHIN(1e-4f, 0.1f, v);
    TEST_ASSERT_FLOAT_WITHIN(1e-4f, 0.5f, w);

    win.reset();
    win.get(&v, &w);
    TEST_ASSERT_FLOAT_WITHIN(kEps, 0.0f, v);
    TEST_ASSERT_FLOAT_WITHIN(kEps, 0.0f, w);
}

int main(int, char**) {
    UNITY_BEGIN();
    RUN_TEST(test_make_geometry);
    RUN_TEST(test_inverse_kinematics_straight);
    RUN_TEST(test_inverse_kinematics_spin);
    RUN_TEST(test_clamp_cmd);
    RUN_TEST(test_omega_to_pwm);
    RUN_TEST(test_scale_pwm_pair);
    RUN_TEST(test_clamp_pwm);
    RUN_TEST(test_wheel_omegas_to_pwm_open_loop);
    RUN_TEST(test_wheel_omegas_to_pwm_p_correction);
    RUN_TEST(test_wheel_omegas_to_pwm_asymmetric);
    RUN_TEST(test_wheel_omegas_to_pwm_asymmetric_scaled);
    RUN_TEST(test_pwm_to_wheel_omega);
    RUN_TEST(test_pos_tracker_first_sample);
    RUN_TEST(test_pos_tracker_first_sample_on_rail_waits);
    RUN_TEST(test_pos_tracker_forward);
    RUN_TEST(test_pos_tracker_backward);
    RUN_TEST(test_pos_tracker_reject_physical_limit);
    RUN_TEST(test_pos_tracker_rail_enters_crossing);
    RUN_TEST(test_pos_tracker_extrapolates_with_measured_when_cmd_zero);
    RUN_TEST(test_pos_tracker_real_right_sequence);
    RUN_TEST(test_pos_tracker_real_left_sequence);
    RUN_TEST(test_pos_tracker_stop_in_deadzone_and_restart);
    RUN_TEST(test_pos_tracker_lost_and_recovery);
    RUN_TEST(test_pos_tracker_read_failure_relocks_with_correction);
    RUN_TEST(test_pos_tracker_stats_and_reset);
    RUN_TEST(test_pos_tracker_vectors);
    RUN_TEST(test_integrate_pose_straight);
    RUN_TEST(test_integrate_pose_spin);
    RUN_TEST(test_integrate_pose_wraps_theta);
    RUN_TEST(test_integrate_pose_quarter_circle);
    RUN_TEST(test_velocity_window);
    return UNITY_END();
}
