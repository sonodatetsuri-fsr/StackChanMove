#pragma once
// ---------------------------------------------------------------------------
// drive_controller — 駆動指令の調停・ウォッチドッグ・PWM 適用。
// loop() から drive_controller_update() を毎周期呼ぶ。
// ---------------------------------------------------------------------------

#include <cstdint>

enum DriveSource : uint8_t {
    DRIVE_SRC_NONE = 0,  // 停止・未指令 (odom の cmd.src="none")
    DRIVE_SRC_HUB,
    DRIVE_SRC_JOYSTICK,
    DRIVE_SRC_AUTO,
    // 校正用の生 PWM (wheel_pwm) を適用中。適用中ソースの表示専用で、
    // drive_source では選べない (parse_source は "calib" を受け付けない)。
    DRIVE_SRC_CALIB,
};

struct DriveState {
    float v;             // 適用中の目標 v [m/s] (クランプ後)
    float w;             // 適用中の目標 w [rad/s]
    DriveSource src;     // 適用中の指令ソース。停止中 (指令なし / タイムアウト) は NONE
    DriveSource source;  // 選択中の駆動ソース (hub / joystick / auto)
    int16_t pwm_l;       // 最後に書き込んだ PWM
    int16_t pwm_r;
    uint32_t last_seq;  // 最後に受理した Hub drive / wheel_pwm の seq
};

/// 内部状態初期化。既定ソースは DRIVE_SRC_JOYSTICK (Hub 未接続でも走行できる)。
void drive_controller_init();

/// Hub からの drive{v, w, seq, src}。src は "hub" | "auto"。
/// 選択中のソースが hub/auto のときだけ適用し、ウォッチドッグを更新する。
void drive_controller_set_hub_cmd(float v, float w, uint32_t seq, const char* src);

/// Hub からの wheel_pwm{l, r, seq} — 校正用の生 PWM。車体前進で正、
/// ±WHEEL_PWM_MAX にクランプする。選択中のソースが hub のときだけ受理し (それ以外は
/// 無視してログを 1 秒に 1 回だけ出す)、drive と同じウォッチドッグ・estop・切断停止が効く。
/// 適用中は逆運動学と PWM 写像を通さず、この値をそのまま wheel_driver_set_pwm() に書く
/// (適用中ソースは DRIVE_SRC_CALIB、cmd.v/w は 0)。drive と wheel_pwm は後から来た方が有効。
void drive_controller_set_hub_pwm(int l, int r, uint32_t seq);

/// Hub からの drive_source{source} ("hub" | "joystick" | "auto")。切替時は一旦停止する。
void drive_controller_set_source(const char* source);

/// ローカル (ジョイスティック) 入力。選択中のソースが joystick のときだけ適用。
/// joystick_update() がリンク中に ≈20 Hz で呼び、リンク断時は 1 回 (0, 0) を入れる。
void drive_controller_set_local_cmd(float v, float w);

/// 非常停止。MiniJoyC のボタン押下エッジで駆動ソースによらず呼ぶ。
/// 即座に wheel_driver_stop() し、選択中ソースを hub に強制して全指令を破棄する。
/// (再びリモコンで走らせるには Hub から drive_source=joystick を選び直す)
void drive_controller_estop();

/// 選択中の駆動ソース (DriveState.source と同じ)。
DriveSource drive_controller_get_source();

/// WebSocket 切断時に呼ぶ。即停止する (Hub 指令を破棄)。
void drive_controller_on_disconnect();

/// loop() から毎周期呼ぶ。ウォッチドッグ (DRIVE_TIMEOUT_MS) の判定と、
/// 逆運動学 → 左右別 PWM 写像 (WHEEL_KP > 0 なら odometry の実測角速度で P 補正) →
/// wheel_driver_set_pwm() を行う。生 PWM (wheel_pwm) の適用中は写像を飛ばして指定値を書く。
/// PWM 書き込みは値が変わったとき (DRIVE_WRITE_MIN_MS 以上
/// 空けて) か DRIVE_WRITE_KEEPALIVE_MS ごとに行う (どちらの経路も共通)。
void drive_controller_update();

/// 現在の状態のコピー。
void drive_controller_get_state(DriveState* out);

/// loop() 停止ガード (odometry タスクから ODOM_SAMPLE_MS ごとに呼ぶ)。
/// drive_controller_update() が DRIVE_TIMEOUT_MS 以上呼ばれておらず PWM が非 0 なら
/// wheel_driver_stop() で即停止する (WS 送受信や再生停止で loop() が長時間ブロックしても
/// 最後の PWM で走り続けないため)。loop() 復帰後の update() で 1 回ログを出す。
void drive_controller_stall_guard(uint32_t now_ms);

/// ウォッチドッグで停止した直後に 1 回だけ true を返す (event{name:"drive_timeout"} 送信用)。
/// 送信できない (未接続) 間は true のまま保持し、呼び出し側が送信に成功したら
/// drive_controller_clear_timeout_event() で消す。
bool drive_controller_timeout_event_pending();
void drive_controller_clear_timeout_event();

/// DriveSource → 文字列 ("hub" / "joystick" / "auto" / "calib" / "none")。
const char* drive_source_str(DriveSource s);
