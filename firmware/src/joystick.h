#pragma once
// ---------------------------------------------------------------------------
// joystick — リモコン入力の適用規則。
//   - リンク中かつ駆動ソースが joystick のとき (v, w) をローカル適用する
//   - リンク断で即座に停止する (デッドマン)
//   - ボタン押下エッジ = 非常停止 (全ソース共通)、長押しは Hub 未接続時のみソース切替
//   - Hub へ送る joy{} / event{} を JSON テキストのキューに積む
// loop() から drive_controller_update() の前に joystick_update() を呼ぶ。
// ---------------------------------------------------------------------------

#include <stddef.h>

/// 内部状態の初期化 (setup() で 1 回)。
void joystick_init();

/// loop() から毎周期呼ぶ。`hub_connected` は WebSocket 接続状態
/// (長押しによるソース切替の可否と joy{} 送信の可否に使う)。
void joystick_update(bool hub_connected);

/// Hub へ送る JSON テキスト ("joy" / "event") を 1 件取り出す。無ければ false。
/// 接続中のみ呼ぶこと (event は未接続の間キューに保持される)。
bool joystick_pop_outgoing(char* buf, size_t len);

/// 診断ログ "[Joy] linked=%d rx=%u/s lost=%u last=%ums" を出す (DIAG_INTERVAL_MS ごと)。
void joystick_diag_report();
