#pragma once
// ---------------------------------------------------------------------------
// net_link — 端末のネットワーク層。
//
// 制御 WS (/ws/device: テキスト双方向 + Hub→端末スピーカー PCM) を net_ctrl タスク、
// メディア WS (/ws/media: 端末→Hub のマイク PCM / カメラ JPEG) を net_media タスクが
// 所有する。loop() はキュー経由でのみやり取りし、ネットワーク I/O で絶対にブロック
// しない。
//   - 受信: net_rx_pop() でテキスト / バイナリを取り出し、処理後 net_rx_free()。
//   - 送信 (制御): net_send_text()。キュー満杯時は最古を捨てて積む (ノンブロッキング)。
//   - 送信 (メディア): net_mic_push() でマイクフレームを積む。カメラは camera_streamer の
//     スロットを net_media が直接取り出して送る。
// ---------------------------------------------------------------------------

#include <stddef.h>
#include <stdint.h>

struct NetRxMsg {
    bool binary;    // true = バイナリフレーム (5 バイトヘッダ込み)、false = テキスト (NUL 終端)
    uint8_t* data;  // ヒープ。net_rx_free() で解放する
    size_t len;     // テキストは NUL を含まない長さ
};

struct NetStats {
    bool ctrl_up;
    bool media_up;
    uint32_t tx_q_len;   // 制御送信キューの現在長
    uint32_t rx_q_len;   // 受信キューの現在長
    uint32_t tx_text;    // 制御テキスト送信成功数 (診断期間)
    uint32_t tx_mic;     // マイクフレーム送信数 (同)
    uint32_t tx_cam;     // カメラフレーム送信数 (同)
    uint32_t odom_skip;  // odom キュー (latest-wins) で捨てた / 未接続で捨てた odom 数 (累積)
    uint32_t tx_drop;    // event / joy を捨てた数: キュー満杯・未接続・再送上限 (累積)
    uint32_t rx_drop;    // 受信キュー満杯で捨てた数 (累積)
    uint32_t mic_drop;   // マイクキュー満杯 / 未接続で捨てた数 (累積)
    uint32_t cam_drop;   // メディア未接続で捨てたカメラフレーム数 (累積)
    uint32_t ctrl_send_max_ms;   // 制御送信 1 回の最大ブロック時間 (診断期間)
    uint32_t media_send_max_ms;  // メディア送信 1 回の最大ブロック時間 (同)
};

/// 制御 WS の URL (NVS の Hub URL、末尾 /ws/device) からメディア WS の URL を導出し、
/// 2 つのタスクを起動する。setup() から 1 回だけ呼ぶ。
void net_init(const char* hub_ctrl_url);

/// 設定 GUI で Hub URL が変わったとき。両タスクが次の周期で切断・再接続する。
void net_set_hub_url(const char* hub_ctrl_url);

bool net_ctrl_connected();
bool net_media_connected();

/// 制御 WS へテキストを積む (ノンブロッキング)。droppable = true (odom) は深さ
/// NET_ODOM_QUEUE_LEN の専用キューで latest-wins (送信が詰まっていれば古い odom を捨てる)。
/// false (event / joy) は NET_TX_QUEUE_LEN のキューで保持する。戻り値は積めたか。
bool net_send_text(const char* json, bool droppable);

/// 受信キューから 1 件取り出す。true なら処理後に必ず net_rx_free() を呼ぶ。
bool net_rx_pop(NetRxMsg* out);
void net_rx_free(NetRxMsg* msg);

/// マイクフレーム (5 バイトヘッダ込み) をメディア送信キューに積む (コピー、ノンブロッキング)。
bool net_mic_push(const uint8_t* frame, size_t len);

/// 統計のスナップショット (期間カウンタはリセットされる)。
void net_get_stats(NetStats* out);

/// "[Net] ..." 診断行を出す (DIAG_INTERVAL_MS ごとに loop() から)。
void net_diag_report();
