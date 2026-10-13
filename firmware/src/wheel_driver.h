#pragma once
// ---------------------------------------------------------------------------
// wheel_driver — SCS0009 (ID 1 / ID 2) を速度開環電機モード (模式 2) で直接駆動する。
// BSP の M5StackChan.begin() / Motion は使わず、PY32 IO エキスパンダで
// サーボ電源 (VM EN) を有効化し、SCSCL を UART1 (1 Mbps, TX=6, RX=7) で初期化する。
// SCSCL へのアクセスはすべて内部ミューテックスで直列化する (loop() から書き込み、
// オドメトリタスクから読み出し)。
// ---------------------------------------------------------------------------

#include <cstdint>

/// 電源有効化・バス初期化・両サーボを模式 2 (PWMMode) に切り替えて停止状態にする。
/// 切り替え前の角度リミット (EEPROM 9〜12) は初回のみ NVS (namespace "wheel") に保存する。
/// 戻り値: 両サーボとの通信に成功したら true。失敗しても以後の API は安全に no-op する。
bool wheel_driver_init();

/// 初期化に成功し両サーボが応答しているか。
bool wheel_driver_ok();

/// 初期化状態の文字列 ("ok" / "io_expander_timeout" / "io_expander_begin" / "uart_begin" /
/// "ping_failed_left" / "ping_failed_right" / "switchmode_failed" / ...)。
/// CoreS3 は USB CDC が起動直後のログを落とすため、定期診断でサーボ初期化の失敗段階を見る用。
const char* wheel_driver_status_str();

/// 左右 PWM (−WHEEL_PWM_MAX..+WHEEL_PWM_MAX、正 = 車体前進方向) を書き込む。
/// 内部で WHEEL_LEFT_SIGN / WHEEL_RIGHT_SIGN を掛けてから WritePWM する。範囲外はクランプ。
void wheel_driver_set_pwm(int16_t left, int16_t right);

/// 両輪 WritePWM(0)。
void wheel_driver_stop();

/// ReadPos を左右 1 回ずつ。読み取り失敗はその側を −1 にする。
/// 戻り値: 両方成功で true。
bool wheel_driver_read_pos(int* left, int* right);

/// ReadSpeed (診断用)。失敗はその側を 0 にし false を返す。
bool wheel_driver_read_speed(int* left, int* right);

/// ReadVoltage (左サーボ、0.1 V 単位、失敗時 −1)。診断用。
int wheel_driver_read_voltage();
