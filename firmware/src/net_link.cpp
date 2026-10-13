#include "net_link.h"

#include <Arduino.h>
#include <WiFi.h>
#include <esp_heap_caps.h>
#include <freertos/FreeRTOS.h>
#include <freertos/queue.h>
#include <freertos/semphr.h>
#include <freertos/task.h>
#include <stdlib.h>
#include <string.h>

#include <atomic>

#include "camera_streamer.h"
#include "config.h"
#include "tx_retry.h"
#include "ws_client.h"

// ---------------------------------------------------------------------------
// キュー要素
// ---------------------------------------------------------------------------
struct TxMsg {
    uint8_t* data;
    size_t len;
    bool droppable;
};

static QueueHandle_t s_rx_q = nullptr;    // NetRxMsg  (net_ctrl → loop)
static QueueHandle_t s_tx_q = nullptr;    // TxMsg     (loop → net_ctrl) event / joy: 捨てない
static QueueHandle_t s_odom_q = nullptr;  // TxMsg     (loop → net_ctrl) odom: latest-wins
static QueueHandle_t s_mic_q = nullptr;   // TxMsg     (loop → net_media)

static WsClient s_ctrl;
static WsClient s_media;

// URL 変更 (loop → 両タスク)
static SemaphoreHandle_t s_url_mux = nullptr;
static String s_ctrl_url;
static String s_media_url;
static std::atomic<uint32_t> s_url_gen{0};
static uint32_t s_ctrl_url_gen = 0;
static uint32_t s_media_url_gen = 0;

// 統計 (書き手は 1 タスクずつ。読み手は loop)
static std::atomic<uint32_t> s_tx_text{0};
static std::atomic<uint32_t> s_tx_mic{0};
static std::atomic<uint32_t> s_tx_cam{0};
static std::atomic<uint32_t> s_odom_skip{0};
static std::atomic<uint32_t> s_tx_drop{0};
static std::atomic<uint32_t> s_rx_drop{0};
static std::atomic<uint32_t> s_mic_drop{0};
static std::atomic<uint32_t> s_cam_drop{0};
static std::atomic<uint32_t> s_ctrl_send_max_ms{0};
static std::atomic<uint32_t> s_media_send_max_ms{0};

static inline void update_max(std::atomic<uint32_t>* slot, uint32_t v) {
    uint32_t cur = slot->load();
    while (v > cur && !slot->compare_exchange_weak(cur, v)) {
    }
}

static void free_tx(TxMsg* m) {
    if (m->data) free(m->data);
    m->data = nullptr;
}

// 制御 WS の event / joy の再送待ち (net_ctrl タスクだけが触る)
static TxMsg s_tx_pending = {nullptr, 0, false};
static uint32_t s_tx_pending_fail = 0;  // s_tx_pending の失敗回数

// ---------------------------------------------------------------------------
// URL 導出: ".../ws/device" → ".../ws/media"。形が違えばメディアは無効 (空)。
// ---------------------------------------------------------------------------
static String derive_media_url(const String& ctrl) {
    const char* suffix = "/ws/device";
    const size_t sl = strlen(suffix);
    if (ctrl.length() < sl || !ctrl.endsWith(suffix)) return String();
    return ctrl.substring(0, ctrl.length() - sl) + "/ws/media";
}

// ---------------------------------------------------------------------------
// 制御 WS の受信コールバック (WS-ctrl クライアントのタスク上。ブロックしない) → 受信キュー
// ---------------------------------------------------------------------------
static void enqueue_rx(bool binary, const uint8_t* data, size_t len) {
    NetRxMsg m;
    m.binary = binary;
    m.len = len;
    if (binary) {
        m.data = static_cast<uint8_t*>(heap_caps_malloc(len, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT));
        if (!m.data) m.data = static_cast<uint8_t*>(malloc(len));
        if (!m.data) {
            ++s_rx_drop;
            return;
        }
        memcpy(m.data, data, len);
    } else {
        m.data = static_cast<uint8_t*>(malloc(len + 1));
        if (!m.data) {
            ++s_rx_drop;
            return;
        }
        memcpy(m.data, data, len);
        m.data[len] = 0;
    }
    if (xQueueSend(s_rx_q, &m, 0) != pdTRUE) {
        free(m.data);
        ++s_rx_drop;
    }
}

static void ctrl_on_text(WsClient*, void*, const char* data, size_t len) {
    enqueue_rx(false, reinterpret_cast<const uint8_t*>(data), len);
}

static void ctrl_on_binary(WsClient*, void*, const uint8_t* data, size_t len) {
    enqueue_rx(true, data, len);
}

static void media_on_text(WsClient*, void*, const char*, size_t) {
    // メディア WS に Hub からテキストは来ない (来ても無視)。
}

static void media_on_binary(WsClient*, void*, const uint8_t*, size_t) {
    // 同上。
}

// ---------------------------------------------------------------------------
// タスク
// ---------------------------------------------------------------------------
static void apply_url_change(WsClient* c, uint32_t* seen_gen, const String& (*pick)()) {
    const uint32_t gen = s_url_gen.load();
    if (gen == *seen_gen) return;
    *seen_gen = gen;
    String url;
    xSemaphoreTake(s_url_mux, portMAX_DELAY);
    url = pick();
    xSemaphoreGive(s_url_mux);
    ws_client_set_url(c, url.c_str());
}

static const String& pick_ctrl_url() {
    return s_ctrl_url;
}
static const String& pick_media_url() {
    return s_media_url;
}

// 制御 WS へ 1 件送る (net_ctrl タスク)。成功したか。
static bool send_ctrl_text(const TxMsg* m) {
    const unsigned long t0 = millis();
    const bool ok = ws_client_send_text(&s_ctrl, reinterpret_cast<const char*>(m->data));
    update_max(&s_ctrl_send_max_ms, static_cast<uint32_t>(millis() - t0));
    return ok;
}

// event / joy を 1 件送る。送信済み・破棄なら m を解放して true、再送待ちなら false
// (m は保持したまま。*fails はこのメッセージの失敗回数)。
static bool try_send_event(TxMsg* m, uint32_t* fails) {
    txr::Action act = txr::Action::kDrop;
    if (ws_client_is_connected(&s_ctrl)) {
        const bool ok = send_ctrl_text(m);
        if (!ok) ++(*fails);
        act = txr::decide(ok, ws_client_is_connected(&s_ctrl), *fails, WS_TX_RETRY_MAX);
    }
    if (act == txr::Action::kRetry) return false;
    if (act == txr::Action::kSent) {
        ++s_tx_text;
    } else {
        ++s_tx_drop;
    }
    free_tx(m);
    return true;
}

static void net_ctrl_task(void*) {
    {
        String url;
        xSemaphoreTake(s_url_mux, portMAX_DELAY);
        url = s_ctrl_url;
        xSemaphoreGive(s_url_mux);
        ws_client_init(&s_ctrl, "WS-ctrl", url.c_str(), WS_CTRL_TASK_PRIO, ctrl_on_text,
                       ctrl_on_binary, nullptr);
        s_ctrl_url_gen = s_url_gen.load();
    }
    for (;;) {
        // 受信はクライアントのタスクが行う。ここは再接続の管理と送信だけ。
        apply_url_change(&s_ctrl, &s_ctrl_url_gen, pick_ctrl_url);
        ws_client_poll(&s_ctrl);

        // 送信キューを掃く: event / joy を先に、odom (latest-wins) を後に。
        // 未接続なら捨てる。送信は TCP の ACK 待ちでブロックし得るが、それはこのタスク
        // の中だけで loop() には波及しない。
        // event / joy は送信に失敗しても接続中なら保持して次の周期で再送し、WS_TX_RETRY_MAX
        // 回失敗したら捨てる。再送待ちがある間は後続を送らない。
        int budget = NET_TX_QUEUE_LEN;
        if (s_tx_pending.data) {
            --budget;
            if (try_send_event(&s_tx_pending, &s_tx_pending_fail)) {
                s_tx_pending_fail = 0;
            }
        }
        TxMsg m;
        while (!s_tx_pending.data && budget-- > 0 && xQueueReceive(s_tx_q, &m, 0) == pdTRUE) {
            uint32_t fails = 0;
            if (!try_send_event(&m, &fails)) {
                s_tx_pending = m;
                s_tx_pending_fail = fails;
            }
        }
        if (xQueueReceive(s_odom_q, &m, 0) == pdTRUE) {
            // odom は再送しない (latest-wins。失敗は ws_send_fail に数えられる)。
            if (ws_client_is_connected(&s_ctrl)) {
                if (send_ctrl_text(&m)) ++s_tx_text;
            } else {
                ++s_odom_skip;
            }
            free_tx(&m);
        }
        vTaskDelay(1);
    }
}

static void net_media_task(void*) {
    {
        String url;
        xSemaphoreTake(s_url_mux, portMAX_DELAY);
        url = s_media_url;
        xSemaphoreGive(s_url_mux);
        ws_client_init(&s_media, "WS-media", url.c_str(), WS_MEDIA_TASK_PRIO, media_on_text,
                       media_on_binary, nullptr);
        s_media_url_gen = s_url_gen.load();
    }
    for (;;) {
        apply_url_change(&s_media, &s_media_url_gen, pick_media_url);
        ws_client_poll(&s_media);
        const bool up = ws_client_is_connected(&s_media);

        // マイクフレーム (FIFO)
        TxMsg m;
        int budget = MIC_TX_QUEUE_LEN;
        while (budget-- > 0 && xQueueReceive(s_mic_q, &m, 0) == pdTRUE) {
            if (up) {
                const unsigned long t0 = millis();
                ws_client_send_binary(&s_media, m.data, m.len);
                update_max(&s_media_send_max_ms, static_cast<uint32_t>(millis() - t0));
                ++s_tx_mic;
            } else {
                ++s_mic_drop;
            }
            free_tx(&m);
        }

        // カメラフレーム (latest-wins スロット)
        uint8_t* frame = nullptr;
        size_t len = 0;
        if (camera_streamer_take(&frame, &len)) {
            if (up) {
                const unsigned long t0 = millis();
                ws_client_send_binary(&s_media, frame, len);
                update_max(&s_media_send_max_ms, static_cast<uint32_t>(millis() - t0));
                ++s_tx_cam;
            } else {
                ++s_cam_drop;
            }
            camera_streamer_release(frame);
        }
        vTaskDelay(1);
    }
}

// ---------------------------------------------------------------------------
// API (loop 側)
// ---------------------------------------------------------------------------
void net_init(const char* hub_ctrl_url) {
    if (s_rx_q != nullptr) {
        net_set_hub_url(hub_ctrl_url);
        return;
    }
    s_url_mux = xSemaphoreCreateMutex();
    s_ctrl_url = hub_ctrl_url ? hub_ctrl_url : "";
    s_media_url = derive_media_url(s_ctrl_url);
    if (s_media_url.length() == 0) {
        Serial.printf("[Net] media url not derivable from %s (media disabled)\n",
                      s_ctrl_url.c_str());
    } else {
        Serial.printf("[Net] ctrl=%s media=%s\n", s_ctrl_url.c_str(), s_media_url.c_str());
    }
    s_rx_q = xQueueCreate(NET_RX_QUEUE_LEN, sizeof(NetRxMsg));
    s_tx_q = xQueueCreate(NET_TX_QUEUE_LEN, sizeof(TxMsg));
    s_odom_q = xQueueCreate(NET_ODOM_QUEUE_LEN, sizeof(TxMsg));
    s_mic_q = xQueueCreate(MIC_TX_QUEUE_LEN, sizeof(TxMsg));
    s_url_gen = 1;
    xTaskCreatePinnedToCore(net_ctrl_task, "net_ctrl", NET_CTRL_TASK_STACK, nullptr,
                            NET_CTRL_TASK_PRIO, nullptr, NET_CTRL_TASK_CORE);
    xTaskCreatePinnedToCore(net_media_task, "net_media", NET_MEDIA_TASK_STACK, nullptr,
                            NET_MEDIA_TASK_PRIO, nullptr, NET_MEDIA_TASK_CORE);
}

void net_set_hub_url(const char* hub_ctrl_url) {
    if (s_url_mux == nullptr) return;
    xSemaphoreTake(s_url_mux, portMAX_DELAY);
    s_ctrl_url = hub_ctrl_url ? hub_ctrl_url : "";
    s_media_url = derive_media_url(s_ctrl_url);
    xSemaphoreGive(s_url_mux);
    ++s_url_gen;
    Serial.printf("[Net] hub url -> %s\n", s_ctrl_url.c_str());
}

bool net_ctrl_connected() {
    return ws_client_is_connected(&s_ctrl);
}

bool net_media_connected() {
    return ws_client_is_connected(&s_media);
}

bool net_send_text(const char* json, bool droppable) {
    if (!json || s_tx_q == nullptr) return false;
    const size_t len = strlen(json);
    TxMsg m;
    m.len = len;
    m.droppable = droppable;
    m.data = static_cast<uint8_t*>(malloc(len + 1));
    if (!m.data) {
        ++s_tx_drop;
        return false;
    }
    memcpy(m.data, json, len + 1);
    // droppable (odom) は専用の浅いキューで latest-wins、それ以外は深いキューで保持する。
    // どちらも満杯なら最古を捨てて積む (ノンブロッキング)。
    QueueHandle_t q = droppable ? s_odom_q : s_tx_q;
    std::atomic<uint32_t>* drop_counter = droppable ? &s_odom_skip : &s_tx_drop;
    if (xQueueSend(q, &m, 0) == pdTRUE) return true;
    TxMsg old;
    if (xQueueReceive(q, &old, 0) == pdTRUE) {
        free_tx(&old);
        ++(*drop_counter);
    }
    if (xQueueSend(q, &m, 0) == pdTRUE) return true;
    free_tx(&m);
    ++(*drop_counter);
    return false;
}

bool net_rx_pop(NetRxMsg* out) {
    if (!out || s_rx_q == nullptr) return false;
    return xQueueReceive(s_rx_q, out, 0) == pdTRUE;
}

void net_rx_free(NetRxMsg* msg) {
    if (msg && msg->data) {
        free(msg->data);
        msg->data = nullptr;
    }
}

bool net_mic_push(const uint8_t* frame, size_t len) {
    if (!frame || len == 0 || s_mic_q == nullptr) return false;
    if (!net_media_connected()) {
        ++s_mic_drop;
        return false;
    }
    TxMsg m;
    m.len = len;
    m.droppable = true;
    m.data = static_cast<uint8_t*>(malloc(len));
    if (!m.data) {
        ++s_mic_drop;
        return false;
    }
    memcpy(m.data, frame, len);
    if (xQueueSend(s_mic_q, &m, 0) == pdTRUE) return true;
    TxMsg old;
    if (xQueueReceive(s_mic_q, &old, 0) == pdTRUE) {
        free_tx(&old);
        ++s_mic_drop;
    }
    if (xQueueSend(s_mic_q, &m, 0) == pdTRUE) return true;
    free_tx(&m);
    ++s_mic_drop;
    return false;
}

void net_get_stats(NetStats* out) {
    if (!out) return;
    out->ctrl_up = net_ctrl_connected();
    out->media_up = net_media_connected();
    out->tx_q_len = s_tx_q ? static_cast<uint32_t>(uxQueueMessagesWaiting(s_tx_q)) : 0;
    if (s_odom_q) out->tx_q_len += static_cast<uint32_t>(uxQueueMessagesWaiting(s_odom_q));
    out->rx_q_len = s_rx_q ? static_cast<uint32_t>(uxQueueMessagesWaiting(s_rx_q)) : 0;
    out->tx_text = s_tx_text.exchange(0);
    out->tx_mic = s_tx_mic.exchange(0);
    out->tx_cam = s_tx_cam.exchange(0);
    out->odom_skip = s_odom_skip.load();
    out->tx_drop = s_tx_drop.load();
    out->rx_drop = s_rx_drop.load();
    out->mic_drop = s_mic_drop.load();
    out->cam_drop = s_cam_drop.load();
    out->ctrl_send_max_ms = s_ctrl_send_max_ms.exchange(0);
    out->media_send_max_ms = s_media_send_max_ms.exchange(0);
}

void net_diag_report() {
    NetStats st;
    net_get_stats(&st);
    Serial.printf(
        "[Net] ctrl=%s media=%s tx_q=%u/%u rx_q=%u/%u tx(text=%u mic=%u cam=%u) odom_skip=%u "
        "tx_drop=%u rx_drop=%u mic_drop=%u cam_drop=%u ctrl_send_max=%ums media_send_max=%ums\n",
        st.ctrl_up ? "up" : "down", st.media_up ? "up" : "down", static_cast<unsigned>(st.tx_q_len),
        static_cast<unsigned>(NET_TX_QUEUE_LEN), static_cast<unsigned>(st.rx_q_len),
        static_cast<unsigned>(NET_RX_QUEUE_LEN), static_cast<unsigned>(st.tx_text),
        static_cast<unsigned>(st.tx_mic), static_cast<unsigned>(st.tx_cam),
        static_cast<unsigned>(st.odom_skip), static_cast<unsigned>(st.tx_drop),
        static_cast<unsigned>(st.rx_drop), static_cast<unsigned>(st.mic_drop),
        static_cast<unsigned>(st.cam_drop), static_cast<unsigned>(st.ctrl_send_max_ms),
        static_cast<unsigned>(st.media_send_max_ms));
    WsClientStats c;
    WsClientStats m;
    ws_client_get_stats(&s_ctrl, &c);
    ws_client_get_stats(&s_media, &m);
    Serial.printf(
        "[Net] ws_rx(ctrl: frames=%u frags=%u oversize=%u desync=%u nomem=%u) "
        "ws_rx(media: frames=%u frags=%u oversize=%u desync=%u nomem=%u) "
        "ws_send_fail(ctrl=%u media=%u) reconnects(ctrl=%u media=%u)\n",
        static_cast<unsigned>(c.frames), static_cast<unsigned>(c.frags),
        static_cast<unsigned>(c.oversize), static_cast<unsigned>(c.desync),
        static_cast<unsigned>(c.nomem), static_cast<unsigned>(m.frames),
        static_cast<unsigned>(m.frags), static_cast<unsigned>(m.oversize),
        static_cast<unsigned>(m.desync), static_cast<unsigned>(m.nomem),
        static_cast<unsigned>(c.send_fail), static_cast<unsigned>(m.send_fail),
        static_cast<unsigned>(c.reconnects), static_cast<unsigned>(m.reconnects));
    // WiFi STA の実態 (接続先 AP / IP / 電波) — WS が繋がらないときの切り分け用。
    Serial.printf("[WiFi] status=%d ssid=%s bssid=%s ch=%d rssi=%d ip=%s gw=%s\n",
                  static_cast<int>(WiFi.status()), WiFi.SSID().c_str(), WiFi.BSSIDstr().c_str(),
                  WiFi.channel(), WiFi.RSSI(), WiFi.localIP().toString().c_str(),
                  WiFi.gatewayIP().toString().c_str());
}
