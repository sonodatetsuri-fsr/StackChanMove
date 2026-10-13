#pragma once
// ---------------------------------------------------------------------------
// joy_math.h — スティック値の正規化 (リモコン側) と (v, w) 写像
// (端末側)。Arduino に依存しない純粋な C++ (header-only)。
// firmware の native テスト (`pio test -e native`) で検証する。
//
// 座標規約: x = 左が正、y = 前が正、いずれも [-1, 1]。
// ---------------------------------------------------------------------------

#include <cmath>
#include <cstdint>

namespace joym {

inline float clamp1(float v) {
    return v > 1.0f ? 1.0f : (v < -1.0f ? -1.0f : v);
}

/// MiniJoyC の POS 値 (10 bit、**符号付き** −512..511、中立 ≈ 0。ライブラリは uint16_t で
/// 返すので呼び出し側で int16_t にキャストして渡す) を中央 `center` 基準で [-1, 1] に
/// 正規化する。`half_range` は片側の振れ幅 (10 bit なら 512)。範囲外はクランプ。
inline float axis_from_pos(float pos, float center, float half_range = 512.0f) {
    if (half_range <= 0.0f) return 0.0f;
    return clamp1((pos - center) / half_range);
}

/// デッドゾーン (半径判定) とエクスポを適用する。
///   - r = hypot(x, y) < deadzone → (0, 0)
///   - それ以外は r を (r − dz) / (1 − dz) に再スケールして連続にし、各軸に
///     x' = (1 − e)·x + e·x³ を掛ける。結果は [-1, 1] にクランプ。
inline void shape_axes(float* x, float* y, float deadzone, float expo) {
    float ax = clamp1(*x);
    float ay = clamp1(*y);
    const float r = std::sqrt(ax * ax + ay * ay);
    if (deadzone > 0.0f) {
        if (r < deadzone) {
            *x = 0.0f;
            *y = 0.0f;
            return;
        }
        if (deadzone < 1.0f && r > 0.0f) {
            const float scale = ((r - deadzone) / (1.0f - deadzone)) / r;
            ax *= scale;
            ay *= scale;
        }
    }
    if (expo > 0.0f) {
        ax = (1.0f - expo) * ax + expo * ax * ax * ax;
        ay = (1.0f - expo) * ay + expo * ay * ay * ay;
    }
    *x = clamp1(ax);
    *y = clamp1(ay);
}

/// 正規化スティック (x 左+, y 前+) → 目標 (v [m/s], w [rad/s])。
/// v = y·max_v、w = x·max_w。w の符号は v の符号に依らない。
inline void stick_to_cmd(float x, float y, float max_v, float max_w, float* v, float* w) {
    *v = clamp1(y) * max_v;
    *w = clamp1(x) * max_w;
}

}  // namespace joym
