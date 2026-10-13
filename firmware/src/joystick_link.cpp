#include "joystick_link.h"

#include <Arduino.h>
#include <WiFi.h>
#include <esp_now.h>
#include <esp_wifi.h>
#include <freertos/FreeRTOS.h>
#include <string.h>

#include "config.h"
#include "joy_protocol.h"

// ---------------------------------------------------------------------------
// 状態。受信コールバック (WiFi タスク) と loop() で共有するものは
// スピンロック s_mux で保護する。
// ---------------------------------------------------------------------------
static portMUX_TYPE s_mux = portMUX_INITIALIZER_UNLOCKED;

static bool s_inited = false;
static uint8_t s_peer_mac[6] = {0, 0, 0, 0, 0, 0};
static bool s_has_peer = false;
static uint8_t s_channel = 0;

// latest-wins スロット
static JoyLinkSample s_slot;
static bool s_slot_new = false;

static volatile uint32_t s_last_joy_ms = 0;
static uint16_t s_last_seq = 0;
static bool s_seq_valid = false;

// 統計
static uint32_t s_rx_window = 0;  // 前回 get_stats() からの受信数
static uint32_t s_lost = 0;
static uint32_t s_dup = 0;
static uint32_t s_tx_fail = 0;
static uint32_t s_stats_ms = 0;
static volatile uint32_t s_rx_raw = 0;       // 診断用: コールバック総数
static volatile uint32_t s_rx_discover = 0;  // 診断用: 有効 DISCOVER 数

// 受信コールバックから loop() への依頼 (ACK 送信)
static bool s_ack_pending = false;
static uint8_t s_ack_mac[6] = {0, 0, 0, 0, 0, 0};

// リンク up/down エッジ
static bool s_linked_prev = false;
static bool s_event_pending = false;
static bool s_event_up = false;
static uint8_t s_event_mac[6] = {0, 0, 0, 0, 0, 0};

// ---------------------------------------------------------------------------
// ヘルパ
// ---------------------------------------------------------------------------
void joystick_link_mac_str(const uint8_t* mac, char* out, size_t len) {
    if (!out || len == 0) return;
    if (!mac) {
        out[0] = '\0';
        return;
    }
    snprintf(out, len, "%02X:%02X:%02X:%02X:%02X:%02X", mac[0], mac[1], mac[2], mac[3], mac[4],
             mac[5]);
}

static uint8_t current_channel() {
    uint8_t primary = 0;
    wifi_second_chan_t second = WIFI_SECOND_CHAN_NONE;
    if (esp_wifi_get_channel(&primary, &second) != ESP_OK) return 0;
    return primary;
}

/// ピアを登録する (既存なら何もしない)。
static bool add_peer(const uint8_t* mac) {
    if (esp_now_is_peer_exist(mac)) return true;
    esp_now_peer_info_t peer;
    memset(&peer, 0, sizeof(peer));
    memcpy(peer.peer_addr, mac, 6);
    peer.channel = 0;  // 0 = 現在のチャネル
    peer.ifidx = WIFI_IF_STA;
#if JOY_ESPNOW_ENCRYPT
    uint8_t key[joyp::kKeyLen];
    joyp::derive_key(JOY_PAIR_KEY, key);
    memcpy(peer.lmk, key, joyp::kKeyLen);
    peer.encrypt = true;
#else
    peer.encrypt = false;
#endif
    const esp_err_t err = esp_now_add_peer(&peer);
    if (err != ESP_OK) {
        Serial.printf("[Joy] add_peer failed (0x%X)\n", static_cast<unsigned>(err));
        return false;
    }
    return true;
}

static void send_ack(const uint8_t* mac);

// ---------------------------------------------------------------------------
// ESP-NOW コールバック (WiFi タスク文脈)。検証とスロット格納だけを行う
// (例外: 既知ピアへの ACK 返信は即時性のためここで送る)。
// ---------------------------------------------------------------------------
static void on_recv(const uint8_t* mac, const uint8_t* data, int len) {
    if (!mac || !data || len < 0) return;
    ++s_rx_raw;
    joyp::Packet p;
    if (!joyp::decode(data, static_cast<size_t>(len), &p)) return;

    if (p.type == joyp::TYPE_DISCOVER) {
        if (p.aux != joyp::pair_key_low16(JOY_PAIR_KEY)) return;
        ++s_rx_discover;
        portENTER_CRITICAL(&s_mux);
        const bool same = s_has_peer && memcmp(mac, s_peer_mac, 6) == 0;
        const bool accept = !s_has_peer || JOY_ALLOW_REPAIR || same;
        if (accept) {
            memcpy(s_ack_mac, mac, 6);
            s_ack_pending = true;
        }
        portEXIT_CRITICAL(&s_mux);
        if (same) {
            // 既知ピアの再探索 (LOST からの復帰) はピア登録済みなので、loop() の遅延
            // (ws_poll のブロック) を待たずここで即 ACK を返す。loop() 側でも再送するが
            // リモコンは LINKED 後の重複 ACK を捨てる。
            send_ack(mac);
        }
        return;
    }

    if (p.type != joyp::TYPE_JOY) return;  // ACK は端末が送る側なので無視

    const uint32_t now = millis();
    portENTER_CRITICAL(&s_mux);
    if (!s_has_peer || memcmp(mac, s_peer_mac, 6) != 0) {
        portEXIT_CRITICAL(&s_mux);
        return;  // 未ペア / 別のリモコン
    }
    // リンク断を跨いだら seq を追い直す (リモコン再起動で seq が 0 に巻き戻っても
    // seq_is_newer() の半周判定で永久に捨て続けないため)。
    const bool track = s_seq_valid && (now - s_last_joy_ms) <= JOY_LINK_TIMEOUT_MS;
    if (track && !joyp::seq_is_newer(p.seq, s_last_seq)) {
        ++s_dup;  // 重複 / 順序逆転は捨てる
        portEXIT_CRITICAL(&s_mux);
        return;
    }
    if (track) s_lost += joyp::seq_gap(p.seq, s_last_seq);
    s_last_seq = p.seq;
    s_seq_valid = true;
    s_slot.x = joyp::axis_from_wire(p.x);
    s_slot.y = joyp::axis_from_wire(p.y);
    s_slot.btn = (p.btn & 0x01) != 0;
    s_slot.flags = p.flags;
    s_slot.vbat_mv = p.aux;
    s_slot.seq = p.seq;
    s_slot.rx_ms = now;
    s_slot_new = true;
    s_last_joy_ms = now;
    ++s_rx_window;
    portEXIT_CRITICAL(&s_mux);
}

static void on_sent(const uint8_t* mac, esp_now_send_status_t status) {
    (void)mac;
    if (status != ESP_NOW_SEND_SUCCESS) ++s_tx_fail;
}

// ---------------------------------------------------------------------------
// ペアリング応答 (loop 文脈)
// ---------------------------------------------------------------------------
static void send_ack(const uint8_t* mac) {
    joyp::Packet p;
    p.type = joyp::TYPE_ACK;
    p.aux = joyp::pair_key_low16(JOY_PAIR_KEY);
    p.chan = s_channel;
    uint8_t buf[joyp::kPacketLen];
    joyp::encode(p, buf);
    esp_now_send(mac, buf, sizeof(buf));
}

static void handle_discover(const uint8_t* mac) {
    s_channel = current_channel();

    bool has_peer = false;
    bool same = false;
    portENTER_CRITICAL(&s_mux);
    has_peer = s_has_peer;
    same = has_peer && memcmp(mac, s_peer_mac, 6) == 0;
    portEXIT_CRITICAL(&s_mux);

    // 既にペア済みの同じリモコンからの DISCOVER (LINKED 中の keepalive、2 秒ごと) は、
    // 再登録も seq リセットもログも行わず静かに ACK だけ返す。
    if (same) {
        send_ack(mac);
        return;
    }

    if (has_peer && !same) {
#if !JOY_ALLOW_REPAIR
        return;  // 既に別のリモコンで固定済み
#else
        esp_now_del_peer(s_peer_mac);
#endif
    }

    if (!add_peer(mac)) return;
    portENTER_CRITICAL(&s_mux);
    memcpy(s_peer_mac, mac, 6);
    s_has_peer = true;
    // DISCOVER はリモコンの起動 / 再探索を意味する。seq を追い直す。
    s_seq_valid = false;
    s_slot_new = false;
    portEXIT_CRITICAL(&s_mux);

    send_ack(mac);
    char mac_str[18];
    joystick_link_mac_str(mac, mac_str, sizeof(mac_str));
    Serial.printf("[Joy] discover from %s -> ack ch=%u\n", mac_str,
                  static_cast<unsigned>(s_channel));
}

// ---------------------------------------------------------------------------
// API
// ---------------------------------------------------------------------------
void joystick_link_init() {
    if (s_inited) {
        esp_now_unregister_recv_cb();
        esp_now_unregister_send_cb();
        esp_now_deinit();
        s_inited = false;
    }

    const esp_err_t err = esp_now_init();
    if (err != ESP_OK) {
        Serial.printf("[Joy] esp_now_init failed (0x%X)\n", static_cast<unsigned>(err));
        return;
    }
#if JOY_ESPNOW_ENCRYPT
    uint8_t pmk[joyp::kKeyLen];
    joyp::derive_key(JOY_PAIR_KEY, pmk);
    esp_now_set_pmk(pmk);
#endif
#if JOY_WIFI_PS_NONE
    // リモコン (非接続端末) からのブロードキャストを取りこぼさないためモデムスリープを切る。
    // 音声への影響を確認すること (config.h のコメント参照)。
    {
        const esp_err_t ps = esp_wifi_set_ps(WIFI_PS_NONE);
        Serial.printf("[Joy] wifi power save OFF (JOY_WIFI_PS_NONE) rc=0x%X\n",
                      static_cast<unsigned>(ps));
    }
#endif
    esp_now_register_recv_cb(on_recv);
    esp_now_register_send_cb(on_sent);

    s_channel = current_channel();
    if (s_has_peer) add_peer(s_peer_mac);  // deinit でピアが消えるため再登録する

    // 再初期化直後はまだ 1 つも受けていないのでリンク断から始める (誤って
    // linked を維持したまま走り続けないため)。
    const uint32_t now = millis();
    portENTER_CRITICAL(&s_mux);
    s_seq_valid = false;
    s_slot_new = false;
    portEXIT_CRITICAL(&s_mux);
    s_last_joy_ms = now;
    s_stats_ms = now;
    s_inited = true;
    Serial.printf("[Joy] esp-now ready ch=%u mac=%s\n", static_cast<unsigned>(s_channel),
                  WiFi.macAddress().c_str());
}

bool joystick_link_take(JoyLinkSample* out) {
    if (!out) return false;
    bool got = false;
    portENTER_CRITICAL(&s_mux);
    if (s_slot_new) {
        *out = s_slot;
        s_slot_new = false;
        got = true;
    }
    portEXIT_CRITICAL(&s_mux);
    return got;
}

bool joystick_link_is_linked() {
    if (!s_seq_valid) return false;
    return (millis() - s_last_joy_ms) < JOY_LINK_TIMEOUT_MS;
}

void joystick_link_update() {
    if (!s_inited) return;

    uint8_t mac[6];
    bool pending = false;
    portENTER_CRITICAL(&s_mux);
    if (s_ack_pending) {
        memcpy(mac, s_ack_mac, 6);
        s_ack_pending = false;
        pending = true;
    }
    portEXIT_CRITICAL(&s_mux);
    if (pending) handle_discover(mac);

    const bool linked = joystick_link_is_linked();
    if (linked == s_linked_prev) return;
    s_linked_prev = linked;
    s_event_up = linked;
    s_event_pending = true;
    memcpy(s_event_mac, s_peer_mac, 6);
    char mac_str[18];
    joystick_link_mac_str(s_event_mac, mac_str, sizeof(mac_str));
    if (linked) {
        Serial.printf("[Joy] link up from %s ch=%u\n", mac_str, static_cast<unsigned>(s_channel));
    } else {
        Serial.println("[Joy] link down");
    }
}

bool joystick_link_pop_link_event(bool* up, char* mac_str, size_t len) {
    if (!s_event_pending) return false;
    s_event_pending = false;
    if (up) *up = s_event_up;
    joystick_link_mac_str(s_event_mac, mac_str, len);
    return true;
}

void joystick_link_get_stats(JoyLinkStats* out) {
    if (!out) return;
    const uint32_t now = millis();
    uint32_t rx = 0;
    portENTER_CRITICAL(&s_mux);
    rx = s_rx_window;
    s_rx_window = 0;
    out->lost = s_lost;
    out->dup = s_dup;
    portEXIT_CRITICAL(&s_mux);

    const uint32_t elapsed = now - s_stats_ms;
    s_stats_ms = now;
    out->rx_per_s = (elapsed > 0) ? ((rx * 1000u + elapsed / 2) / elapsed) : 0;
    out->linked = joystick_link_is_linked();
    out->last_ms = now - s_last_joy_ms;
    out->tx_fail = s_tx_fail;
    out->raw = s_rx_raw;
    out->discover = s_rx_discover;
    out->channel = current_channel();
    out->has_peer = s_has_peer;
}

const uint8_t* joystick_link_peer_mac() {
    return s_peer_mac;
}

bool joystick_link_has_peer() {
    return s_has_peer;
}

uint8_t joystick_link_channel() {
    return s_channel;
}
