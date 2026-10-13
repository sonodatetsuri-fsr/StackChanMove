#pragma once
// ---------------------------------------------------------------------------
// tx_retry — 制御 WS の event / joy の再送の判定。header-only・ESP-IDF /
// Arduino 非依存 (native テスト可)。
//
// 送信に失敗しても接続中なら捨てずに次の周期で同じメッセージを再送し、retry_max 回失敗
// したら捨てる。再送待ちがある間、呼び出し側は後続のメッセージを送らない (順序を保つ)。
// odom (latest-wins) には使わない。
// ---------------------------------------------------------------------------

#include <stdint.h>

// 古い config.h (改訂 1 以前の写し) でもビルドできるように既定値を置く。
// config.h を先に include すること (config.h の値が優先される)。
#ifndef WS_TX_RETRY_MAX
#define WS_TX_RETRY_MAX 3
#endif

namespace txr {

enum class Action : uint8_t {
    kSent,   // 送信済み (解放してよい)
    kRetry,  // 保持して次の周期で同じメッセージを再送する
    kDrop,   // 捨てる (tx_drop を数える)
};

/// 1 回の送信結果から次の扱いを決める。
/// sent: 今回の送信が成功したか。connected: 送信後に接続中か。
/// failures: 今回の失敗を含めた、このメッセージの失敗回数。retry_max: 捨てるまでの失敗回数。
inline Action decide(bool sent, bool connected, uint32_t failures, uint32_t retry_max) {
    if (sent) return Action::kSent;
    if (!connected) return Action::kDrop;
    if (failures >= retry_max) return Action::kDrop;
    return Action::kRetry;
}

}  // namespace txr
