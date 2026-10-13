#pragma once
// ---------------------------------------------------------------------------
// ws_client — ESP-IDF esp_websocket_client (Apache-2.0、Arduino コア同梱) のラッパ
// 制御用 (/ws/device) とメディア用 (/ws/media) の 2 本を同じ実装で扱う。
//
// スレッド安全性:
// - 受信はクライアントごとの専用タスク (esp_websocket_client が作る) 上のイベントハンドラで
//   断片を組み立て (ws_reassembly.h)、完成したフレームでコールバックを呼ぶ。
// - init / set_url / poll (再接続の管理) は所有タスク (net_link の net_ctrl / net_media)
//   からのみ呼ぶ。stop / destroy はイベントハンドラの中では呼ばない。
// - 送信は esp_websocket_client 内部のロックで保護される。呼び出しは所有タスクから行う。
// - `connected` と診断カウンタは他タスクから読める (std::atomic)。
// ---------------------------------------------------------------------------

#include <Arduino.h>
#include <esp_websocket_client.h>

#include <atomic>

#include "config.h"
#include "ws_reassembly.h"

// 古い config.h (esp_websocket_client 導入前の写し) でもビルドできるように既定値を置く。
#ifndef WS_CTRL_TASK_PRIO
#define WS_CTRL_TASK_PRIO 2
#endif
#ifndef WS_MEDIA_TASK_PRIO
#define WS_MEDIA_TASK_PRIO 1
#endif
#ifndef WS_TASK_STACK
#define WS_TASK_STACK 6144
#endif
#ifndef WS_RX_BUFFER_SIZE
#define WS_RX_BUFFER_SIZE 4096
#endif
#ifndef WS_RX_MAX_FRAME
#define WS_RX_MAX_FRAME 131072
#endif
#ifndef WS_RX_INLINE_MAX
#define WS_RX_INLINE_MAX 512
#endif
#ifndef WS_SEND_TIMEOUT_MS
#define WS_SEND_TIMEOUT_MS 3000
#endif
#ifndef WS_PING_INTERVAL_S
#define WS_PING_INTERVAL_S 10
#endif

struct WsClient;

/// 受信テキスト (NUL 終端) / バイナリのコールバック。クライアントのタスク上で呼ばれるため、
/// ブロックしないこと (data はコールバックの間だけ有効)。
typedef void (*WsClientTextCb)(WsClient* c, void* ctx, const char* data, size_t len);
typedef void (*WsClientBinaryCb)(WsClient* c, void* ctx, const uint8_t* data, size_t len);

/// 受信の統計 (ws_reassembly の Stats の写し) と送信失敗・再接続の回数。
struct WsClientStats {
    uint32_t frames;
    uint32_t frags;
    uint32_t oversize;
    uint32_t desync;
    uint32_t nomem;
    uint32_t send_fail;
    uint32_t reconnects;
};

struct WsClient {
    esp_websocket_client_handle_t handle = nullptr;
    String url;
    const char* tag = "WS";
    int task_prio = 1;
    bool inited = false;
    std::atomic<bool> need_reconnect{false};
    unsigned long last_attempt_ms = 0;
    unsigned long backoff_ms = WS_RECONNECT_INTERVAL_MS;
    std::atomic<bool> connected{false};
    std::atomic<unsigned long> down_ms{0};  // 最後に切断 / 終了 / エラーのイベントを受けた時刻
    WsClientTextCb on_text = nullptr;
    WsClientBinaryCb on_binary = nullptr;
    void* ctx = nullptr;

    // 受信の組み立て (クライアントのタスクだけが触る。destroy 後は所有タスクが reset する)
    wsr::Reassembler<WS_RX_INLINE_MAX> rx;
    // 診断 (読み手は loop)
    std::atomic<uint32_t> send_fail{0};
    std::atomic<uint32_t> reconnects{0};
};

/// 初期化と最初の接続開始 (所有タスクから呼ぶ)。task_prio はクライアントのタスクの優先度。
void ws_client_init(WsClient* c, const char* tag, const char* url, int task_prio,
                    WsClientTextCb on_text, WsClientBinaryCb on_binary, void* ctx);

/// URL を差し替えて即座に再接続を始める (所有タスクから呼ぶ)。
void ws_client_set_url(WsClient* c, const char* url);

/// 指数バックオフの再接続 (destroy → init → start)。所有タスクから毎周期呼ぶ。
void ws_client_poll(WsClient* c);

/// 送信 (所有タスクから)。未接続・失敗なら false。最大 WS_SEND_TIMEOUT_MS ブロックし得る。
bool ws_client_send_text(WsClient* c, const char* json);
bool ws_client_send_binary(WsClient* c, const uint8_t* data, size_t len);

/// 接続中か (任意のタスクから読める)。
bool ws_client_is_connected(const WsClient* c);

/// 診断の取得 (任意のタスクから。値は累計。組み立ての統計は厳密な同時刻の写しではない)。
void ws_client_get_stats(const WsClient* c, WsClientStats* out);
