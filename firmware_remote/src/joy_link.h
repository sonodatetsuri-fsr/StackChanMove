#pragma once
// ---------------------------------------------------------------------------
// joy_link — ESP-NOW リンク: チャネルホッピング探索・ピア保存・送信
//
// リモコンは WiFi 資格情報を持たない (AP には接続しない)。CoreS3 の STA チャネル
// はルータ依存で不定なので、NVS に保存したチャネル → 1〜13 の順で DISCOVER を
// ブロードキャストし、ACK を返した端末をユニキャストピアとして登録する。
// 探索は loop() から駆動する協調的状態機械で、LCD とボタンを止めない。
// ---------------------------------------------------------------------------

#include <stdint.h>

#include "joy_protocol.h"

enum JoyLinkState : uint8_t {
    JOY_LINK_SEARCH = 0,  ///< 未リンク (起動直後 / 再ペアリング後) で探索中
    JOY_LINK_LINKED,      ///< ピア確定、JOY 送信中
    JOY_LINK_LOST,        ///< 一度リンクしたが送達失敗が続き、再探索中
};

struct JoyLinkStats {
    uint32_t tx_total = 0;     ///< JOY 送信試行数
    uint32_t tx_ok = 0;        ///< 送達コールバックが成功を返した数
    uint32_t tx_fail = 0;      ///< 同 失敗数 (同期エラーを含む)
    uint32_t fail_streak = 0;  ///< 連続失敗数 (JOY_LINK_FAIL_N で LOST)
    uint32_t discover_tx = 0;  ///< DISCOVER 送信数 (探索の進行確認用)
    uint32_t ack_rx = 0;       ///< 有効な ACK 受信数
    uint32_t chan_err = 0;     ///< esp_wifi_set_channel の失敗数
    uint32_t lost_events = 0;  ///< LINKED → LOST に落ちた回数
    uint32_t relink_joy = 0;   ///< LOST 中の JOY 送達成功で LINKED に戻った回数
};

/// 無線が実際にいるチャネル (esp_wifi_get_channel)。s_chan と食い違えば設定失敗。
uint8_t joy_link_radio_channel();

/// WiFi STA + ESP-NOW を初期化し、NVS のピア情報を読んで探索を開始する。
bool joy_link_begin();

/// 探索の状態機械とリンク断判定を進める。loop() から毎回呼ぶ。
void joy_link_update();

/// LINKED のときだけピアへ JOY パケットを送る。送信を試みたら true。
bool joy_link_send_joy(const joyp::Packet& p);

JoyLinkState joy_link_state();

/// 表示・ログ用の状態名 ("SEARCH" / "LINKED" / "LOST")。
const char* joy_link_state_name();

/// 現在の無線チャネル (探索中は走査中のチャネル)。
uint8_t joy_link_channel();

/// ピア MAC (6 バイト)。未確定でも 0 埋めの配列を返す。
const uint8_t* joy_link_peer_mac();
bool joy_link_has_peer();

/// NVS のピア情報を消して先頭から再探索する (BtnA 長押し)。
void joy_link_repair();

const JoyLinkStats& joy_link_stats();
