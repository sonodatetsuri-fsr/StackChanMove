#pragma once
// ---------------------------------------------------------------------------
// camera_streamer — CoreS3 内蔵カメラ (GC0308) の QVGA RGB565 を Core 0 の専用タスクで
// ソフトウェア JPEG 化し、単一スロット (latest-wins) に置く。
// loop() が camera_streamer_take() で取り出して kind=0x02 のバイナリフレームとして送る。
// スロットのフレームは 5 バイトヘッダ [kind u8][ts_ms u32 LE] を含む。
// ---------------------------------------------------------------------------

#include <stddef.h>
#include <stdint.h>

/// esp_camera_init() と配信タスクの起動。成功で true。
/// 失敗しても他機能は継続する (配信のみ無効)。
bool camera_streamer_init();

/// camera_streamer_init() が成功したか。
bool camera_streamer_is_ready();

/// WS 接続時に呼ぶ。配信を有効化する (実際に送るかは set_config の on にも従う)。
void camera_streamer_start();

/// WS 切断時に呼ぶ。タスクを待機させ、未送信スロットを破棄する。
void camera_streamer_stop();

/// Hub の camera{on,fps,quality} を反映する。
/// fps は CAM_FPS_MIN..CAM_FPS_MAX、quality は CAM_QUALITY_MIN..CAM_QUALITY_MAX にクランプ。
void camera_streamer_set_config(bool on, int fps, int quality);

/// スロットのフレーム (ヘッダ込み) の所有権を受け取る。無ければ false。
/// 取得したポインタは camera_streamer_release() で解放すること。
bool camera_streamer_take(uint8_t** frame, size_t* len);

/// camera_streamer_take() で得たフレームを解放する。
void camera_streamer_release(uint8_t* frame);

/// 診断ログ "[Cam] fps=%.1f jpeg=%uB drop=%u" を出す (DIAG_INTERVAL_MS ごと)。
void camera_streamer_diag_report();
