#pragma once
// ---------------------------------------------------------------------------
// joy_input — Hat MiniJoyC の読み取り・中央校正・正規化・LED
//
// I2C (SDA=G0 / SCL=G26, addr 0x54) で MiniJoyC を読み、座標
// 規約 (x = 左が正、y = 前が正、いずれも [-1, 1]) に正規化した値を返す。
// ---------------------------------------------------------------------------

#include <stdint.h>

/// 1 回分の読み取り結果。
struct JoyReading {
    float x = 0.0f;      ///< 正規化 x (左が正、デッドゾーン・エクスポ適用済み)
    float y = 0.0f;      ///< 正規化 y (前が正、同上)
    bool btn = false;    ///< ボタン押下中 (JOY_BTN_ACTIVE_LOW 適用済み)
    int16_t raw_x = 0;   ///< POS_X の生値 (符号付き 10 bit、−512..511、中立 ≈ 0)
    int16_t raw_y = 0;   ///< POS_Y の生値 (同上)
    bool valid = false;  ///< MiniJoyC が使用可能なとき true
};

/// MiniJoyC を初期化する。成功時 true。失敗時は 1 秒ごとに再試行してよい。
bool joy_input_begin();

/// begin() が成功して読み取り可能な状態か。
bool joy_input_ready();

/// 中央値を JOY_CENTER_SAMPLES 回の平均 (または内蔵校正値) で決める。
void joy_input_calibrate();

/// 中央校正が完了しているか (パケットの FLAG_CALIBRATED に載せる)。
bool joy_input_calibrated();

/// スティックとボタンを 1 回読む。未初期化なら valid=false の中立値を返す。
JoyReading joy_input_read();

/// MiniJoyC の RGB LED を設定する (同色の連続書き込みは I2C を出さない)。
void joy_input_set_led(uint32_t rgb888);

/// 起動時に読んだファームウェア版。
uint8_t joy_input_fw_version();

/// 中央校正値 (診断表示用)。
float joy_input_center_x();
float joy_input_center_y();
