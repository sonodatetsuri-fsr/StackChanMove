#pragma once
// ---------------------------------------------------------------------------
// diffdrive_math.h — 差動二輪の運動学・PWM 写像・位置フィードバック積算
//
// Arduino / FreeRTOS に依存しない純粋な C++ (header-only)。ホスト側の単体テスト
// (`pio test -e native`) で検証する。単位: 長さ [m]、角度 [rad]、時間 [s]。
// ---------------------------------------------------------------------------

#include <cmath>
#include <cstdint>

namespace ddm {

constexpr float kPi = 3.14159265358979323846f;

// ---------------------------------------------------------------------------
// Geometry
// ---------------------------------------------------------------------------
struct Geometry {
    float wheel_radius_m;  // 車輪半径 [m]
    float track_m;         // トレッド [m]
};

inline Geometry make_geometry(float wheel_diameter_mm, float track_width_mm) {
    return Geometry{wheel_diameter_mm / 2000.0f, track_width_mm / 1000.0f};
}

// ---------------------------------------------------------------------------
// Command clamp / inverse kinematics
// ---------------------------------------------------------------------------
inline float clampf(float x, float lo, float hi) {
    return x < lo ? lo : (x > hi ? hi : x);
}

/// (v, w) を ±max_v / ±max_w に個別クランプする。
inline void clamp_cmd(float* v, float* w, float max_v, float max_w) {
    *v = clampf(*v, -max_v, max_v);
    *w = clampf(*w, -max_w, max_w);
}

/// 目標 (v [m/s], w [rad/s]) → 左右車輪角速度 (wl, wr) [rad/s]。
/// vl = v − w·T/2, vr = v + w·T/2, ω = v_wheel / r。
inline void inverse_kinematics(const Geometry& g, float v, float w, float* wl, float* wr) {
    float vl = v - w * g.track_m * 0.5f;
    float vr = v + w * g.track_m * 0.5f;
    *wl = vl / g.wheel_radius_m;
    *wr = vr / g.wheel_radius_m;
}

/// 左右車輪移動量 (dl, dr) [m] → (ds, dth)。
inline void forward_kinematics(const Geometry& g, float dl, float dr, float* ds, float* dth) {
    *ds = (dl + dr) * 0.5f;
    *dth = (dr - dl) / g.track_m;
}

// ---------------------------------------------------------------------------
// PWM mapping
// ---------------------------------------------------------------------------
/// 片側 (左または右) の PWM 写像パラメータ。左右別に持つ。
struct PwmMap {
    int deadband;     // 静止摩擦を超えない PWM 幅 (WHEEL_PWM_DEADBAND_L/R)
    float per_rad_s;  // フィードフォワード係数 [PWM/(rad/s)] (WHEEL_PWM_PER_RAD_S_L/R)
    int pwm_max;      // 絶対値上限 (WHEEL_PWM_MAX)。左右で同じ値を入れる
    float min_omega;  // これ未満の |ω| は 0 (WHEEL_MIN_OMEGA_RADPS)
};

/// ω [rad/s] → PWM (符号付き)。|ω| < min_omega なら 0。
/// pwm = sign(ω)·(deadband + |ω|·per_rad_s)。上限クランプはしない (scale_pwm_pair で行う)。
inline int32_t omega_to_pwm(const PwmMap& m, float omega) {
    float a = std::fabs(omega);
    if (a < m.min_omega) return 0;
    float mag = static_cast<float>(m.deadband) + a * m.per_rad_s;
    int32_t p = static_cast<int32_t>(mag + 0.5f);
    return omega < 0 ? -p : p;
}

/// PWM を ±pwm_max にクランプする (生 PWM 指令用)。
inline int32_t clamp_pwm(int32_t p, int pwm_max) {
    const int32_t m = static_cast<int32_t>(pwm_max);
    if (p > m) return m;
    if (p < -m) return -m;
    return p;
}

/// |l|,|r| のいずれかが pwm_max を超えるとき、比率を保って両方を縮小する。
inline void scale_pwm_pair(int32_t* l, int32_t* r, int pwm_max) {
    int32_t al = *l < 0 ? -*l : *l;
    int32_t ar = *r < 0 ? -*r : *r;
    int32_t peak = al > ar ? al : ar;
    if (peak <= pwm_max) return;
    float k = static_cast<float>(pwm_max) / static_cast<float>(peak);
    *l = static_cast<int32_t>(std::lround(static_cast<float>(*l) * k));
    *r = static_cast<int32_t>(std::lround(static_cast<float>(*r) * k));
}

/// 目標角速度 (wl, wr) と (任意の) 実測角速度から左右 PWM を決める。
/// 写像は左右別 (map_l / map_r) で、デッドバンドと係数のサーボ個体差を吸収する。
/// kp > 0 かつ meas_valid のときは P 補正 pwm += kp·(ω_target − ω_meas) を加える。
/// 上限縮小 (scale_pwm_pair) は左右共通の上限で行うため、map_l.pwm_max と map_r.pwm_max には
/// 同じ値 (WHEEL_PWM_MAX) を入れること。ここでは map_l.pwm_max を使う。
inline void wheel_omegas_to_pwm(const PwmMap& map_l, const PwmMap& map_r, float wl, float wr,
                                float kp, bool l_meas_valid, float wl_meas, bool r_meas_valid,
                                float wr_meas, int32_t* pwm_l, int32_t* pwm_r) {
    int32_t pl = omega_to_pwm(map_l, wl);
    int32_t pr = omega_to_pwm(map_r, wr);
    if (kp > 0.0f) {
        if (pl != 0 && l_meas_valid) pl += static_cast<int32_t>(std::lround(kp * (wl - wl_meas)));
        if (pr != 0 && r_meas_valid) pr += static_cast<int32_t>(std::lround(kp * (wr - wr_meas)));
    }
    scale_pwm_pair(&pl, &pr, map_l.pwm_max);
    *pwm_l = pl;
    *pwm_r = pr;
}

// ---------------------------------------------------------------------------
// PWM 逆写像 — 適用中の PWM から指令車輪角速度 ω_cmd を逆算する
// ---------------------------------------------------------------------------
/// PWM (符号付き) → ω [rad/s]。ω = sign(pwm)·max(0, |pwm| − deadband) / per_rad_s。
/// omega_to_pwm() の逆 (丸め分を除く)。tools/sim_device.py の _omega_from_pwm と同式。
inline float pwm_to_wheel_omega(int32_t pwm, const PwmMap& m) {
    if (m.per_rad_s <= 0.0f) return 0.0f;
    int32_t a = pwm < 0 ? -pwm : pwm;
    int32_t over = a - static_cast<int32_t>(m.deadband);
    if (over <= 0) return 0.0f;
    float mag = static_cast<float>(over) / m.per_rad_s;
    return pwm < 0 ? -mag : mag;
}

// ---------------------------------------------------------------------------
// Wheel position tracker v2 — ReadPos からの回転角積算
//
// SCS0009 の ReadPos は機械角 0..valid_deg (300°) を 0..steps-1 (1023) で返し、残りの
// 不感帯 (実測 ≈ 40°、WHEEL_DEAD_DEG) では飽和端の値・中間値・単発のゴミを返す。
// 3 状態の機械で「物理的に整合する差分」だけを積算し、不感帯の横断中は
// 指令 ω (ω_cmd、PWM 逆写像) で外挿する (停止指令中は直近の実測 ω を fresh_s で減衰)。
//   locked   : 直前の読み値と連続。差分が範囲内かつ |Δθ| ≤ omega_max·dt なら採用。
//   crossing : 予測位置 pred を ω_est で進め、読み値が予測位置 (±relock_tol_step) と
//              差分 (ω_est·dt ± tol/2) の両方に relock_samples 回連続で整合したら再ロック
//              (再ロック時に予測との残差を補正として積算 → 横断の合計角は幾何どおり)。
//              外挿の累積が lost_deg を超えたら lost。
//   lost     : 指令 ω のみで積算し、物理上限内の差分が relock_samples+1 回続けば再ロック。
// 内部演算は double (C++ と Python 実装 (movehub.services.pos_tracker) の一致を
// テストベクタで担保するため)。単位: 角度 [rad]、時間 [s]。
// ---------------------------------------------------------------------------
struct PosTrackerConfig {
    int steps;              // WHEEL_POS_STEPS (1024)
    float valid_deg;        // WHEEL_POS_VALID_DEG (300)
    int valid_min;          // WHEEL_POS_VALID_MIN: p ≤ valid_min は飽和端 (locked では採用しない)
    int valid_max;          // WHEEL_POS_VALID_MAX: p ≥ valid_max は飽和端
    float omega_max;        // WHEEL_OMEGA_PHYS_MAX_RADPS: |Δθ| > omega_max·dt の差分は棄却
    float dead_deg;         // WHEEL_DEAD_DEG: 不感帯の機械角 (予測位置の折り返し周期に使う)
    int relock_tol_step;    // WHEEL_RELOCK_TOL_STEP: 予測位置との許容差 [step]
    int relock_samples;     // WHEEL_RELOCK_SAMPLES: 再ロックに必要な連続整合サンプル数
    float lost_deg;         // WHEEL_LOST_DEG: 横断中の外挿累積がこれを超えたら lost
    float fresh_s;          // WHEEL_OMEGA_FRESH_MS/1000: 停止指令中に実測 ω を 0 へ減衰させる時間
    float omega_ema_alpha;  // WHEEL_OMEGA_EMA_ALPHA: 実測 ω の指数平滑係数 (1 = 平滑なし)
};

inline PosTrackerConfig make_pos_tracker_config(int steps, float valid_deg, int valid_min,
                                                int valid_max, float omega_max = 12.0f,
                                                float dead_deg = 40.0f, int relock_tol_step = 60,
                                                int relock_samples = 2, float lost_deg = 120.0f,
                                                float fresh_s = 0.04f,
                                                float omega_ema_alpha = 0.3f) {
    PosTrackerConfig c{steps,     valid_deg, valid_min,       valid_max,
                       omega_max, dead_deg,  relock_tol_step, relock_samples,
                       lost_deg,  fresh_s,   omega_ema_alpha};
    return c;
}

enum class PosState : uint8_t { Locked = 0, Crossing = 1, Lost = 2 };
enum class PosSrc : uint8_t { None = 0, Meas = 1, Cmd = 2 };

inline const char* pos_state_str(PosState s) {
    switch (s) {
        case PosState::Locked:
            return "locked";
        case PosState::Crossing:
            return "crossing";
        default:
            return "lost";
    }
}

inline char pos_state_char(PosState s) {
    switch (s) {
        case PosState::Locked:
            return 'L';
        case PosState::Crossing:
            return 'C';
        default:
            return 'X';
    }
}

inline const char* pos_src_str(PosSrc s) {
    switch (s) {
        case PosSrc::Meas:
            return "meas";
        case PosSrc::Cmd:
            return "cmd";
        default:
            return "none";
    }
}

struct PosTrackerStats {
    uint32_t cross = 0;       // 飽和端で不感帯に入った回数
    uint32_t lost = 0;        // crossing → lost の回数
    uint32_t reject = 0;      // 範囲内だが物理上限で棄却した差分の数
    double extrap_rad = 0.0;  // 外挿で積算した角の累積絶対値 [rad] (再ロック補正は含まない)
};

class PosTracker {
   public:
    PosTracker() { configure(cfg_); }
    explicit PosTracker(const PosTrackerConfig& cfg) { configure(cfg); }

    void configure(const PosTrackerConfig& cfg) {
        cfg_ = cfg;
        step_rad_ =
            (static_cast<double>(cfg_.valid_deg) / static_cast<double>(cfg_.steps)) * kPiD / 180.0;
        rev_steps_ = static_cast<double>(cfg_.steps) *
                     (static_cast<double>(cfg_.valid_deg) + static_cast<double>(cfg_.dead_deg)) /
                     static_cast<double>(cfg_.valid_deg);
        lost_rad_ = static_cast<double>(cfg_.lost_deg) * kPiD / 180.0;
        reset();
    }

    /// 状態を初期化する (統計は保持)。
    void reset() {
        state_ = PosState::Lost;
        src_ = PosSrc::None;
        prev_pos_ = -1;
        prev_raw_ = -1;
        omega_meas_ = 0.0;
        have_meas_ = false;
        since_meas_ = 0.0;
        pred_ = 0.0;
        cross_acc_ = 0.0;
        cand_ = 0;
        ever_locked_ = false;
    }

    bool is_in_range(int pos) const { return pos > cfg_.valid_min && pos < cfg_.valid_max; }

    /// 1 サンプル更新。pos は ReadPos (−1 = 失敗)、dt は前回からの時間 [s]、
    /// omega_cmd は指令車輪角速度 [rad/s] (ReadPos の計数方向で符号付き)。
    /// 戻り値: このサンプルで積算する Δθ [rad]。
    float update(int pos, float dt_s, float omega_cmd) {
        const double dt = static_cast<double>(dt_s);
        const double wcmd = static_cast<double>(omega_cmd);
        const bool in_range = pos >= 0 && is_in_range(pos);
        const bool rail = pos >= 0 && !in_range;
        if (have_meas_) since_meas_ += dt;
        double dtheta = 0.0;
        src_ = PosSrc::None;

        if (state_ == PosState::Locked) {
            bool adopted = false;
            if (in_range) {
                const double d = static_cast<double>(pos - prev_pos_);
                const double max_step = (static_cast<double>(cfg_.omega_max) * dt) / step_rad_;
                if (std::fabs(d) <= max_step) {
                    dtheta = d * step_rad_;
                    if (dt > 0.0) {
                        const double inst = dtheta / dt;
                        const double a = static_cast<double>(cfg_.omega_ema_alpha);
                        omega_meas_ = have_meas_ ? (omega_meas_ + a * (inst - omega_meas_)) : inst;
                        have_meas_ = true;
                        since_meas_ = 0.0;
                    }
                    prev_pos_ = pos;
                    src_ = PosSrc::Meas;
                    adopted = true;
                } else {
                    ++stats_.reject;
                }
            } else if (rail) {
                ++stats_.cross;
            }
            if (!adopted) {
                // 横断開始: 予測位置を最後の実測読み値から始め、このサンプルは外挿する。
                state_ = PosState::Crossing;
                pred_ = static_cast<double>(prev_pos_);
                cross_acc_ = 0.0;
                cand_ = 0;
                dtheta = crossing_step(pos, in_range, dt, wcmd);
            }
        } else if (state_ == PosState::Crossing) {
            dtheta = crossing_step(pos, in_range, dt, wcmd);
        } else {
            dtheta = lost_step(pos, in_range, dt, wcmd);
        }
        prev_raw_ = pos;
        return static_cast<float>(dtheta);
    }

    PosState state() const { return state_; }
    PosSrc src() const { return src_; }
    bool last_valid() const { return state_ == PosState::Locked; }
    bool has_omega() const { return have_meas_ && state_ == PosState::Locked; }
    float omega() const { return static_cast<float>(omega_meas_); }  // 平滑化した実測 ω [rad/s]
    const PosTrackerStats& stats() const { return stats_; }
    double pred_pos() const { return wrap_rev(pred_); }  // 診断用: 予測位置 [step, 0..rev)
    double last_relock_step() const { return last_relock_step_; }  // 診断用: 直近の再ロック残差

   private:
    static constexpr double kPiD = 3.14159265358979323846;

    double wrap_rev(double p) const {
        double r = std::fmod(p, rev_steps_);
        if (r < 0.0) r += rev_steps_;
        return r;
    }

    void lock(int pos) {
        state_ = PosState::Locked;
        prev_pos_ = pos;
        cand_ = 0;
        ever_locked_ = true;
    }

    double crossing_step(int pos, bool in_range, double dt, double wcmd) {
        // 外挿する ω: 指令 ω があればそれ (サーボは PWM に 1〜2 サンプルで追従し、停止からの
        // 発進で実測 ω = 0 を使い続けると予測が遅れて再ロックできない)。停止指令中は
        // 直近の実測 ω を fresh_s で 0 まで線形に減衰させて使う (実測: PWM 0 で 1 サンプル
        // 以内に停止する)。それも無ければ 0。
        double w_est;
        if (wcmd != 0.0) {
            w_est = wcmd;
            src_ = PosSrc::Cmd;
        } else if (have_meas_ && since_meas_ < static_cast<double>(cfg_.fresh_s)) {
            w_est = omega_meas_ * (1.0 - since_meas_ / static_cast<double>(cfg_.fresh_s));
            src_ = PosSrc::Meas;
        } else {
            w_est = 0.0;
        }
        if (w_est == 0.0) src_ = PosSrc::None;
        const double ext = w_est * dt;
        double dtheta = ext;
        pred_ += ext / step_rad_;
        cross_acc_ += std::fabs(ext);
        stats_.extrap_rad += std::fabs(ext);

        bool ok = false;
        double dpos = 0.0;
        if (in_range) {
            dpos = static_cast<double>(pos) - wrap_rev(pred_);
            if (dpos > rev_steps_ * 0.5) dpos -= rev_steps_;
            if (dpos <= -rev_steps_ * 0.5) dpos += rev_steps_;
            const double tol = static_cast<double>(cfg_.relock_tol_step);
            const bool pos_ok = std::fabs(dpos) <= tol;
            bool diff_ok = false;
            if (prev_raw_ >= 0) {
                const double dd = static_cast<double>(pos - prev_raw_) - ext / step_rad_;
                diff_ok = std::fabs(dd) <= tol * 0.5;
            }
            ok = pos_ok && diff_ok;
        }
        if (ok) {
            ++cand_;
            if (cand_ >= cfg_.relock_samples) {
                // 再ロック: 予測との残差を補正として積算し、以後は読み値の差分に戻る。
                dtheta += dpos * step_rad_;
                last_relock_step_ = dpos;
                lock(pos);
                return dtheta;
            }
        } else {
            cand_ = 0;
        }
        if (cross_acc_ > lost_rad_) {
            state_ = PosState::Lost;
            ++stats_.lost;
            cand_ = 0;
        }
        return dtheta;
    }

    double lost_step(int pos, bool in_range, double dt, double wcmd) {
        double dtheta = 0.0;
        if (!ever_locked_ && in_range) {
            // 起動直後: 最初の範囲内読み値でそのままロックする。
            lock(pos);
            return 0.0;
        }
        dtheta = wcmd * dt;
        src_ = (wcmd != 0.0) ? PosSrc::Cmd : PosSrc::None;
        stats_.extrap_rad += std::fabs(dtheta);
        if (in_range && prev_raw_ >= 0 && is_in_range(prev_raw_)) {
            const double d = static_cast<double>(pos - prev_raw_);
            const double max_step = (static_cast<double>(cfg_.omega_max) * dt) / step_rad_;
            if (std::fabs(d) <= max_step) {
                ++cand_;
                if (cand_ >= cfg_.relock_samples + 1) lock(pos);
                return dtheta;
            }
        }
        cand_ = 0;
        return dtheta;
    }

    PosTrackerConfig cfg_{1024, 300.0f, 12, 1012, 12.0f, 40.0f, 60, 2, 120.0f, 0.04f, 0.3f};
    double step_rad_ = 0.0;
    double rev_steps_ = 0.0;
    double lost_rad_ = 0.0;
    PosState state_ = PosState::Lost;
    PosSrc src_ = PosSrc::None;
    int prev_pos_ = -1;  // 最後にロック状態で採用した読み値
    int prev_raw_ = -1;  // 直前の生読み値 (−1 = 失敗)
    double omega_meas_ = 0.0;
    bool have_meas_ = false;
    double since_meas_ = 0.0;
    double pred_ = 0.0;
    double cross_acc_ = 0.0;
    int cand_ = 0;
    bool ever_locked_ = false;
    double last_relock_step_ = 0.0;
    PosTrackerStats stats_;
};

// ---------------------------------------------------------------------------
// Pose integrator — (dl, dr) → (x, y, θ)、θ は −π..π に正規化
// ---------------------------------------------------------------------------
inline float wrap_pi(float a) {
    while (a > kPi) a -= 2.0f * kPi;
    while (a < -kPi) a += 2.0f * kPi;
    return a;
}

struct Pose {
    float x = 0.0f;
    float y = 0.0f;
    float th = 0.0f;
};

/// 中点法で姿勢を積算する。戻り値は (ds, dth)。
inline void integrate_pose(const Geometry& g, Pose* p, float dl, float dr, float* ds_out,
                           float* dth_out) {
    float ds, dth;
    forward_kinematics(g, dl, dr, &ds, &dth);
    float mid = p->th + dth * 0.5f;
    p->x += ds * std::cos(mid);
    p->y += ds * std::sin(mid);
    p->th = wrap_pi(p->th + dth);
    if (ds_out) *ds_out = ds;
    if (dth_out) *dth_out = dth;
}

// ---------------------------------------------------------------------------
// Moving-average velocity over the last N samples
// ---------------------------------------------------------------------------
template <int N>
class VelocityWindow {
   public:
    void reset() {
        for (int i = 0; i < N; ++i) {
            ds_[i] = 0.0f;
            dth_[i] = 0.0f;
            dt_[i] = 0.0f;
        }
        idx_ = 0;
    }
    void push(float ds, float dth, float dt) {
        ds_[idx_] = ds;
        dth_[idx_] = dth;
        dt_[idx_] = dt;
        idx_ = (idx_ + 1) % N;
    }
    /// v [m/s], w [rad/s]。窓内の合計時間が 0 なら 0。
    void get(float* v, float* w) const {
        float s = 0, t = 0, d = 0;
        for (int i = 0; i < N; ++i) {
            s += ds_[i];
            t += dth_[i];
            d += dt_[i];
        }
        if (d <= 0.0f) {
            *v = 0.0f;
            *w = 0.0f;
            return;
        }
        *v = s / d;
        *w = t / d;
    }

   private:
    float ds_[N] = {};
    float dth_[N] = {};
    float dt_[N] = {};
    int idx_ = 0;
};

}  // namespace ddm
