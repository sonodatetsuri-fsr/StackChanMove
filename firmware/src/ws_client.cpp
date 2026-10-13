#include "ws_client.h"

#include <esp_heap_caps.h>
#include <stdlib.h>

// 切断 / 終了イベントから destroy までの猶予。自動再接続を切った IDF 4.4 版は、
// DISCONNECTED / CLOSED を出した後に自分のタスクを畳む (run=false → transport close →
// STOPPED_BIT → vTaskDelete)。この間に destroy すると stop() が待たずに戻り、解放済みの
// クライアントにタスクが触れるため、畳み終わるまで待つ。
static constexpr unsigned long kTeardownGraceMs = 200;

// ---------------------------------------------------------------------------
// 組み立てバッファ (PSRAM) と完成フレームの受け渡し
// ---------------------------------------------------------------------------
static void* rx_alloc(size_t n) {
    void* p = heap_caps_malloc(n, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
    if (!p) p = malloc(n);
    return p;
}

static void rx_free(void* p) {
    free(p);
}

static void on_frame(void* ctx, uint8_t op, const uint8_t* data, size_t len) {
    WsClient* c = static_cast<WsClient*>(ctx);
    if (op == wsr::kOpText) {
        if (c->on_text) c->on_text(c, c->ctx, reinterpret_cast<const char*>(data), len);
    } else if (op == wsr::kOpBinary) {
        if (c->on_binary) c->on_binary(c, c->ctx, data, len);
    }
}

// ---------------------------------------------------------------------------
// イベントハンドラ (クライアントのタスク上)。stop / destroy はここでは呼ばない。
// ---------------------------------------------------------------------------
static void mark_down(WsClient* c, const char* what) {
    const bool was_up = c->connected.exchange(false);
    c->down_ms = millis();
    c->need_reconnect = true;
    if (was_up) {
        Serial.printf("[%s] Disconnected (%s)\n", c->tag, what);
    } else {
        Serial.printf("[%s] Connection failed (%s)\n", c->tag, what);
    }
}

static void ws_event_handler(void* arg, esp_event_base_t, int32_t event_id, void* event_data) {
    WsClient* c = static_cast<WsClient*>(arg);
    const esp_websocket_event_data_t* d =
        static_cast<const esp_websocket_event_data_t*>(event_data);
    switch (event_id) {
        case WEBSOCKET_EVENT_CONNECTED:
            Serial.printf("[%s] Connected\n", c->tag);
            c->rx.reset();
            c->need_reconnect = false;
            c->connected = true;
            break;
        case WEBSOCKET_EVENT_DISCONNECTED:
            mark_down(c, "disconnected");
            break;
        case WEBSOCKET_EVENT_CLOSED:
            mark_down(c, "closed");
            break;
        case WEBSOCKET_EVENT_ERROR:
            mark_down(c, "error");
            break;
        case WEBSOCKET_EVENT_DATA:
            if (d && d->payload_len >= 0 && d->payload_offset >= 0 && d->data_len >= 0) {
                c->rx.feed(d->op_code, reinterpret_cast<const uint8_t*>(d->data_ptr),
                           static_cast<size_t>(d->data_len), static_cast<size_t>(d->payload_len),
                           static_cast<size_t>(d->payload_offset));
            }
            break;
        default:
            break;
    }
}

// ---------------------------------------------------------------------------
// 生成と破棄 (所有タスクから)
// ---------------------------------------------------------------------------
static void destroy_client(WsClient* c) {
    if (c->handle) {
        // destroy は内部で stop (タスクの終了を待つ) を呼ぶ。
        esp_websocket_client_destroy(c->handle);
        c->handle = nullptr;
    }
    c->connected = false;
    c->rx.reset();
}

static bool start_client(WsClient* c) {
    Serial.printf("[%s] Connecting to %s ...\n", c->tag, c->url.c_str());
    esp_websocket_client_config_t cfg = {};
    cfg.uri = c->url.c_str();  // init 内で複写される
    cfg.disable_auto_reconnect = true;
    cfg.task_prio = c->task_prio;
    cfg.task_stack = WS_TASK_STACK;
    cfg.buffer_size = WS_RX_BUFFER_SIZE;
    cfg.ping_interval_sec = WS_PING_INTERVAL_S;
    c->handle = esp_websocket_client_init(&cfg);
    if (!c->handle) {
        Serial.printf("[%s] client init failed\n", c->tag);
        return false;
    }
    esp_websocket_register_events(c->handle, WEBSOCKET_EVENT_ANY, ws_event_handler, c);
    if (esp_websocket_client_start(c->handle) != ESP_OK) {
        Serial.printf("[%s] client start failed\n", c->tag);
        destroy_client(c);
        return false;
    }
    return true;
}

// ---------------------------------------------------------------------------
// API
// ---------------------------------------------------------------------------
void ws_client_init(WsClient* c, const char* tag, const char* url, int task_prio,
                    WsClientTextCb on_text, WsClientBinaryCb on_binary, void* ctx) {
    destroy_client(c);
    c->tag = tag ? tag : "WS";
    c->url = url ? url : "";
    c->task_prio = task_prio;
    c->on_text = on_text;
    c->on_binary = on_binary;
    c->ctx = ctx;
    c->backoff_ms = WS_RECONNECT_INTERVAL_MS;
    c->rx.init(WS_RX_MAX_FRAME, rx_alloc, rx_free, on_frame, c);
    c->inited = true;
    c->last_attempt_ms = millis();
    if (c->url.length() == 0) {
        c->need_reconnect = false;
        return;
    }
    c->need_reconnect = false;
    if (!start_client(c)) c->need_reconnect = true;
}

void ws_client_set_url(WsClient* c, const char* url) {
    destroy_client(c);
    c->url = url ? url : "";
    c->backoff_ms = WS_RECONNECT_INTERVAL_MS;
    c->need_reconnect = c->url.length() > 0;
    c->last_attempt_ms = 0;  // 次の poll で即座に接続を試みる
}

void ws_client_poll(WsClient* c) {
    if (!c->inited) return;
    if (c->connected) {
        c->backoff_ms = WS_RECONNECT_INTERVAL_MS;
        return;
    }
    if (!c->need_reconnect) return;

    const unsigned long now = millis();
    if (c->last_attempt_ms != 0 && now - c->last_attempt_ms < c->backoff_ms) return;
    const unsigned long down_ms = c->down_ms.load();
    if (c->handle && down_ms != 0 && now - down_ms < kTeardownGraceMs) return;

    c->last_attempt_ms = now;
    if (c->handle) {
        destroy_client(c);
        ++c->reconnects;
        // 再接続を繰り返してもメモリが減っていかないこと (リーク無し) の確認用。
        Serial.printf("[Diag] %s reconnect #%u heap=%u psram=%u\n", c->tag,
                      static_cast<unsigned>(c->reconnects.load()),
                      static_cast<unsigned>(heap_caps_get_free_size(MALLOC_CAP_INTERNAL)),
                      static_cast<unsigned>(heap_caps_get_free_size(MALLOC_CAP_SPIRAM)));
        Serial.printf("[%s] Reconnecting (backoff %lu ms)...\n", c->tag, c->backoff_ms);
    }
    // start より前に下ろす (start の直後に失敗イベントが来て立て直すのを上書きしないため)。
    c->need_reconnect = false;
    if (!start_client(c)) c->need_reconnect = true;
    // 次の失敗に備えて間隔を伸ばす (接続できたら上の connected の分岐で初期値に戻る)。
    c->backoff_ms *= 2;
    if (c->backoff_ms > WS_RECONNECT_MAX_INTERVAL_MS) c->backoff_ms = WS_RECONNECT_MAX_INTERVAL_MS;
}

bool ws_client_send_text(WsClient* c, const char* json) {
    if (!json || !c->handle || !c->connected) return false;
    const int len = static_cast<int>(strlen(json));
    const int sent =
        esp_websocket_client_send_text(c->handle, json, len, pdMS_TO_TICKS(WS_SEND_TIMEOUT_MS));
    if (sent != len) {
        ++c->send_fail;
        return false;
    }
    return true;
}

bool ws_client_send_binary(WsClient* c, const uint8_t* data, size_t len) {
    if (!data || len == 0 || !c->handle || !c->connected) return false;
    const int n = static_cast<int>(len);
    const int sent = esp_websocket_client_send_bin(c->handle, reinterpret_cast<const char*>(data),
                                                   n, pdMS_TO_TICKS(WS_SEND_TIMEOUT_MS));
    if (sent != n) {
        ++c->send_fail;
        return false;
    }
    return true;
}

bool ws_client_is_connected(const WsClient* c) {
    return c->connected.load();
}

void ws_client_get_stats(const WsClient* c, WsClientStats* out) {
    if (!out) return;
    out->frames = c->rx.stats.frames;
    out->frags = c->rx.stats.frags;
    out->oversize = c->rx.stats.oversize;
    out->desync = c->rx.stats.desync;
    out->nomem = c->rx.stats.nomem;
    out->send_fail = c->send_fail.load();
    out->reconnects = c->reconnects.load();
}
