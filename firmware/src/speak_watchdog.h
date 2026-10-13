#pragma once
// ---------------------------------------------------------------------------
// speak_watchdog — 発話の見張りの判定。header-only・ESP-IDF / Arduino
// 非依存 (native テスト可)。
//
// 発話中に Hub から音声フレームが timeout_ms 届かなければ、speak_end を受けたときと同じ
// 処理をする。計時の起点は speak_start と、音声フレームを受けた時刻。時刻は millis() の
// 下位 32 bit を想定し、差を uint32_t で取るので折り返し (約 49.7 日) をまたいでも正しい。
// ---------------------------------------------------------------------------

#include <stdint.h>

// 古い config.h (改訂 1 以前の写し) でもビルドできるように既定値を置く。
// config.h を先に include すること (config.h の値が優先される)。
#ifndef SPEAK_RX_TIMEOUT_MS
#define SPEAK_RX_TIMEOUT_MS 5000
#endif

namespace spkwd {

/// 最後に音声を受けてからの経過時間 [ms] (折り返しを考慮)。
inline uint32_t elapsed_ms(uint32_t last_audio_ms, uint32_t now_ms) {
    return static_cast<uint32_t>(now_ms - last_audio_ms);
}

/// 発話を見張りで止めるべきか。speaking: 発話中 (speak_start 後、speak_end / 停止の前)。
/// last_audio_ms: speak_start または最後に音声フレームを受けた時刻。
inline bool should_stop(bool speaking, uint32_t last_audio_ms, uint32_t now_ms,
                        uint32_t timeout_ms) {
    if (!speaking) return false;
    return elapsed_ms(last_audio_ms, now_ms) >= timeout_ms;
}

}  // namespace spkwd
