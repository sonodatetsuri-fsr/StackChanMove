#pragma once

/// アバター顔を LCD に初期化する。
void face_init();

/// 文字列で表情を設定する。
/// 対応表情: "neutral", "happy", "sad", "angry", "doubt", "sleepy"
/// 不明な文字列は neutral にフォールバックする。
void face_set_expression(const char* expr);

/// アバター描画タスクを一時停止する（GUI 設定画面表示用）。
void face_suspend();

/// アバター描画タスクを再開する。
void face_resume();

/// 口パク＋表情アニメーションを開始する（speak_start 時に呼ぶ）
void face_speak_anim_start();

/// 口パク＋表情アニメーションを停止し口を閉じ表情を戻す（speak_end 時に呼ぶ）
void face_speak_anim_stop();

/// loop() から毎フレーム呼ぶ。アニメーション中でなければ即 return。
void face_speak_anim_update();
