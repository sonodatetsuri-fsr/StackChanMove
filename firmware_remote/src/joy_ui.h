#pragma once
// ---------------------------------------------------------------------------
// joy_ui — M5StickC Plus の 1.14" LCD (135x240) 表示
//
// ページ 0 (main): 状態行 / x・y バー / ボタン / バッテリ / 送信成功率
// ページ 1 (stats): ピア MAC・チャネル・送信数の内訳・中央校正値
// BtnB でページを切り替える (駆動には影響しない)。
// ---------------------------------------------------------------------------

#include <stdint.h>

struct JoyUiModel {
    const char* state = "SEARCH";  ///< "SEARCH" / "LINKED" / "LOST"
    uint8_t chan = 0;
    const uint8_t* mac = nullptr;  ///< ピア MAC (6 バイト) / 未確定なら nullptr
    float x = 0.0f;
    float y = 0.0f;
    bool btn = false;
    bool joy_ok = false;  ///< MiniJoyC が読めているか
    uint16_t vbat_mv = 0;
    bool low_bat = false;
    uint32_t tx_total = 0;
    uint32_t tx_ok = 0;
    uint32_t tx_fail = 0;
};

void joy_ui_begin();

/// BtnB で呼ぶ。次のページへ切り替えて全面再描画する。
void joy_ui_next_page();

uint8_t joy_ui_page();

/// 画面を更新する (内部で描画間隔を間引く)。
void joy_ui_update(const JoyUiModel& m);

/// MiniJoyC が見つからないなどの致命的表示。復帰時は自動で通常表示に戻る。
void joy_ui_error(const char* line1, const char* line2);
