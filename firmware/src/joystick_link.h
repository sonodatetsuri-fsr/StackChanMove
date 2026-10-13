#pragma once
// ---------------------------------------------------------------------------
// joystick_link — MiniJoyC リモコン (M5StickC Plus) からの ESP-NOW 受信、
// ペアリング応答 (DISCOVER → ACK) とリンク監視。
//
// 受信コールバックは WiFi タスク上で動くため、検証 (magic / ver / crc / type /
// ピア MAC / seq 単調性) だけを行い、値は latest-wins のスロットに置く。
// loop() から joystick_link_update() / joystick_link_take() で取り出す。
// ---------------------------------------------------------------------------

#include <stddef.h>
#include <stdint.h>

/// 受信した JOY パケット 1 件 (正規化済み)。
struct JoyLinkSample {
    float x;           // 左が正 [-1, 1]
    float y;           // 前が正 [-1, 1]
    bool btn;          // ボタン押下中
    uint8_t flags;     // joyp::FLAG_*
    uint16_t vbat_mv;  // リモコンのバッテリ電圧 [mV]
    uint16_t seq;      // 送信連番
    uint32_t rx_ms;    // 受信時刻 (millis)
};

/// 診断用のリンク統計。
struct JoyLinkStats {
    bool linked;
    uint32_t rx_per_s;  // 前回 get_stats() からの JOY 受信レート [1/s]
    uint32_t lost;      // seq の欠落累積数
    uint32_t dup;       // 重複 / 順序逆転で捨てた数
    uint32_t tx_fail;   // ACK 送信の失敗数
    uint32_t last_ms;   // 最後に有効な JOY を受けてからの経過 [ms]
    uint32_t raw;       // 受信コールバック呼び出し総数 (検証前。電波が届いているかの指標)
    uint32_t discover;  // 検証を通った DISCOVER の総数
    uint32_t channel;   // 現在の STA チャネル
    bool has_peer;      // ペア済みか
};

/// ESP-NOW を初期化して受信コールバックを登録する。WiFi STA 接続後に呼ぶこと。
/// 冪等: 既に初期化済みなら esp_now_deinit() してから初期化し直す (再接続時は
/// STA チャネルが変わりうるため)。ピア登録は保持され、再初期化後に再登録する。
void joystick_link_init();

/// 最新の受信サンプルを取り出す。前回の take 以降に新しい受信があれば true。
bool joystick_link_take(JoyLinkSample* out);

/// 最後に有効な JOY を受けてから JOY_LINK_TIMEOUT_MS 以内なら true。
bool joystick_link_is_linked();

/// loop() から毎周期呼ぶ。保留中の ACK 送信とリンク up/down エッジの判定を行う。
void joystick_link_update();

/// リンク up/down エッジを 1 件取り出す。`mac_str` にはピアの MAC 文字列が入る。
bool joystick_link_pop_link_event(bool* up, char* mac_str, size_t len);

/// 診断統計 (rx/s の窓はこの呼び出しでリセットされる)。
void joystick_link_get_stats(JoyLinkStats* out);

/// ペアリング済みリモコンの MAC (6 バイト)。未ペアなら全 0。
const uint8_t* joystick_link_peer_mac();

/// ペアリング済みか。
bool joystick_link_has_peer();

/// 最後に確認した STA のプライマリチャネル。
uint8_t joystick_link_channel();

/// MAC を "AA:BB:CC:DD:EE:FF" 形式に整形する (len は 18 以上)。
void joystick_link_mac_str(const uint8_t* mac, char* out, size_t len);
