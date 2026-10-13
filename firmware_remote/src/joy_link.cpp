#include "joy_link.h"

#include <Arduino.h>
#include <Preferences.h>
#include <WiFi.h>
#include <esp_now.h>
#include <esp_wifi.h>
#include <string.h>

#include "config.h"

// ---------------------------------------------------------------------------
// 内部状態
// ---------------------------------------------------------------------------
static const uint8_t kBroadcast[6] = {0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF};
static constexpr uint8_t kChanMin = 1;
static constexpr uint8_t kChanMax = 13;
static const char* kPrefsNamespace = "joyc";
static const char* kPrefsKeyMac = "mac";
static const char* kPrefsKeyChan = "chan";

/// 探索フェーズ。NVS のチャネルを先に試し、駄目なら 1〜13 を巡回する。
enum SearchPhase : uint8_t {
    PHASE_NVS = 0,
    PHASE_HOP,
    PHASE_DONE,
};

static JoyLinkState s_state = JOY_LINK_SEARCH;
static SearchPhase s_phase = PHASE_HOP;
static uint8_t s_peer[6] = {0, 0, 0, 0, 0, 0};
static bool s_has_peer = false;
static uint8_t s_chan = kChanMin;  // 現在の無線チャネル
static uint8_t s_stored_chan = 0;  // NVS に保存されていたチャネル
static uint8_t s_try = 0;          // 現チャネルでの DISCOVER 送信回数
static unsigned long s_wait_start = 0;
static unsigned long s_last_keepalive_ms = 0;  // LINKED 中の定期 DISCOVER の最終送信時刻
static uint16_t s_discover_seq = 0;
static bool s_inited = false;
static JoyLinkStats s_stats;

// 受信コールバック (WiFi タスク文脈) → loop() へ渡す ACK スロット
static volatile bool s_ack_pending = false;
static volatile uint8_t s_ack_chan = 0;
static uint8_t s_ack_mac[6] = {0, 0, 0, 0, 0, 0};

// 送達コールバック (WiFi タスク文脈) → loop() へ: LOST 中の JOY が届いた
static volatile bool s_relink_pending = false;
static volatile unsigned long s_last_ok_ms = 0;  // 最後に JOY 送達が成功した時刻

static Preferences s_prefs;

// ---------------------------------------------------------------------------
// ヘルパ
// ---------------------------------------------------------------------------
static void mac_to_str(const uint8_t* mac, char* out, size_t len) {
    snprintf(out, len, "%02X:%02X:%02X:%02X:%02X:%02X", mac[0], mac[1], mac[2], mac[3], mac[4],
             mac[5]);
}

static uint8_t current_channel() {
    uint8_t primary = 0;
    wifi_second_chan_t second = WIFI_SECOND_CHAN_NONE;
    if (esp_wifi_get_channel(&primary, &second) != ESP_OK) {
        return s_chan;
    }
    return primary;
}

static void set_channel(uint8_t ch) {
    if (ch < kChanMin || ch > kChanMax) {
        ch = kChanMin;
    }
    s_chan = ch;
    const esp_err_t err = esp_wifi_set_channel(ch, WIFI_SECOND_CHAN_NONE);
    if (err != ESP_OK) {
        s_stats.chan_err++;
        if (s_stats.chan_err <= 3) {
            Serial.printf("[Link] set_channel(%u) failed (0x%X)\n", static_cast<unsigned>(ch),
                          static_cast<unsigned>(err));
        }
    }
}

static bool add_peer(const uint8_t* mac, bool encrypt) {
    esp_now_peer_info_t info;
    memset(&info, 0, sizeof(info));
    memcpy(info.peer_addr, mac, 6);
    info.ifidx = WIFI_IF_STA;
    info.channel = 0;  // 0 = 現在のチャネルを使う (探索で合わせてある)
    info.encrypt = encrypt;
#if JOY_ESPNOW_ENCRYPT
    if (encrypt) {
        // LMK は共有ヘッダの導出規則で作る (CoreS3 側と必ず同一)。
        joyp::derive_key(JOY_PAIR_KEY, info.lmk);
    }
#endif
    esp_now_del_peer(mac);  // 既存登録があれば作り直す (戻り値は無視してよい)
    return esp_now_add_peer(&info) == ESP_OK;
}

static void save_peer_to_nvs() {
    if (!s_prefs.begin(kPrefsNamespace, false)) {
        return;
    }
    s_prefs.putBytes(kPrefsKeyMac, s_peer, sizeof(s_peer));
    s_prefs.putUChar(kPrefsKeyChan, s_chan);
    s_prefs.end();
}

static void load_peer_from_nvs() {
    s_has_peer = false;
    s_stored_chan = 0;
    if (!s_prefs.begin(kPrefsNamespace, true)) {
        return;
    }
    uint8_t mac[6] = {0, 0, 0, 0, 0, 0};
    const size_t n = s_prefs.getBytes(kPrefsKeyMac, mac, sizeof(mac));
    const uint8_t chan = s_prefs.getUChar(kPrefsKeyChan, 0);
    s_prefs.end();
    if (n == sizeof(mac) && chan >= kChanMin && chan <= kChanMax) {
        memcpy(s_peer, mac, sizeof(mac));
        s_stored_chan = chan;
        s_has_peer = true;
    }
}

static void clear_peer_in_nvs() {
    if (!s_prefs.begin(kPrefsNamespace, false)) {
        return;
    }
    s_prefs.clear();
    s_prefs.end();
}

// ---------------------------------------------------------------------------
// ESP-NOW コールバック (arduino-esp32 2.0.x / ESP-IDF 4.4 のシグネチャ)
// ---------------------------------------------------------------------------
static void on_recv(const uint8_t* mac, const uint8_t* data, int len) {
    if (!mac || !data || len != static_cast<int>(joyp::kPacketLen)) {
        return;
    }
    joyp::Packet p;
    if (!joyp::decode(data, static_cast<size_t>(len), &p)) {
        return;
    }
    if (p.type != joyp::TYPE_ACK) {
        return;
    }
    if (p.aux != joyp::pair_key_low16(JOY_PAIR_KEY)) {
        return;
    }
    memcpy(s_ack_mac, mac, 6);
    s_ack_chan = p.chan;
    s_stats.ack_rx++;
    s_ack_pending = true;
}

static void on_sent(const uint8_t* mac, esp_now_send_status_t status) {
    if (!mac || memcmp(mac, kBroadcast, 6) == 0) {
        return;  // DISCOVER のブロードキャストは常に成功扱いなので集計しない
    }
    if (status == ESP_NOW_SEND_SUCCESS) {
        s_stats.tx_ok++;
        s_stats.fail_streak = 0;
        s_last_ok_ms = millis();
        if (s_state != JOY_LINK_LINKED) {
            s_relink_pending = true;  // LOST/SEARCH 中でも届いた = 端末は同じチャネルにいる
        }
    } else {
        s_stats.tx_fail++;
        s_stats.fail_streak++;
    }
}

// ---------------------------------------------------------------------------
// 探索 (協調的状態機械)
// ---------------------------------------------------------------------------
static void send_discover() {
    joyp::Packet p;
    p.type = joyp::TYPE_DISCOVER;
    p.seq = s_discover_seq++;
    p.aux = joyp::pair_key_low16(JOY_PAIR_KEY);
    p.chan = 0;
    uint8_t buf[joyp::kPacketLen];
    joyp::encode(p, buf);
    s_stats.discover_tx++;
    const esp_err_t err = esp_now_send(kBroadcast, buf, sizeof(buf));
    if (err != ESP_OK && s_stats.discover_tx <= 3) {
        Serial.printf("[Link] discover send failed (0x%X)\n", static_cast<unsigned>(err));
    }
    s_wait_start = millis();
}

/// 探索を先頭 (NVS のチャネル → ホッピング) から開始する。
static void start_search(JoyLinkState state) {
    s_state = state;
    s_stats.fail_streak = 0;
    s_ack_pending = false;
    s_try = 0;
    if (s_has_peer && s_stored_chan >= kChanMin) {
        s_phase = PHASE_NVS;
        set_channel(s_stored_chan);
    } else {
        s_phase = PHASE_HOP;
        set_channel(kChanMin);
    }
    send_discover();
    s_try = 1;
}

/// 現チャネルの試行を 1 回進める。使い切ったら次のチャネル / フェーズへ。
static void advance_search() {
    if (s_try < JOY_DISCOVER_RETRY) {
        send_discover();
        s_try++;
        return;
    }
    s_try = 0;
    if (s_phase == PHASE_NVS) {
        // 保存チャネルで見つからなかった → 1〜13 のホッピングへ
        s_phase = PHASE_HOP;
        set_channel(kChanMin);
    } else {
        uint8_t next = static_cast<uint8_t>(s_chan + 1);
        if (next > kChanMax) {
            next = kChanMin;
        }
        set_channel(next);
    }
    send_discover();
    s_try = 1;
}

static void on_ack_received() {
    uint8_t mac[6];
    memcpy(mac, s_ack_mac, sizeof(mac));
    const uint8_t ack_chan = s_ack_chan;
    s_ack_pending = false;

    const uint8_t chan =
        (ack_chan >= kChanMin && ack_chan <= kChanMax) ? ack_chan : current_channel();
    memcpy(s_peer, mac, sizeof(s_peer));
    s_has_peer = true;
    set_channel(chan);
    if (!add_peer(s_peer, JOY_ESPNOW_ENCRYPT != 0)) {
        Serial.println("[Link] add_peer failed");
        return;  // 次の DISCOVER 待ちに戻る (state は SEARCH/LOST のまま)
    }
    s_stored_chan = chan;
    save_peer_to_nvs();
    s_state = JOY_LINK_LINKED;
    s_phase = PHASE_DONE;
    s_stats.fail_streak = 0;
    s_last_ok_ms = millis();
    s_last_keepalive_ms = millis();
    s_relink_pending = false;

    char macstr[18];
    mac_to_str(s_peer, macstr, sizeof(macstr));
    Serial.printf("[Link] linked %s ch=%u\n", macstr, static_cast<unsigned>(chan));
}

// ---------------------------------------------------------------------------
// 公開 API
// ---------------------------------------------------------------------------
bool joy_link_begin() {
    // リモコンは AP に接続しない。STA モードは ESP-NOW の前提としてだけ使う。
    // 過去のスケッチが NVS に残した AP 設定で勝手に接続・スキャンを始めると
    // esp_wifi_set_channel が効かなくなるため、自動接続を切ってから STA にする。
    WiFi.persistent(false);
    WiFi.setAutoReconnect(false);
    WiFi.mode(WIFI_STA);
    WiFi.disconnect();

    if (esp_now_init() != ESP_OK) {
        Serial.println("[Link] esp_now_init failed");
        return false;
    }
#if JOY_ESPNOW_ENCRYPT
    uint8_t pmk[ESP_NOW_KEY_LEN];
    joyp::derive_key(JOY_PAIR_KEY, pmk);  // CoreS3 側と同一の導出 (shared/joy_protocol.h)
    esp_now_set_pmk(pmk);
#endif
    esp_now_register_recv_cb(on_recv);
    esp_now_register_send_cb(on_sent);

    // DISCOVER 用のブロードキャストピア (channel 0 = 現在のチャネル、暗号化なし)
    esp_now_peer_info_t bcast;
    memset(&bcast, 0, sizeof(bcast));
    memcpy(bcast.peer_addr, kBroadcast, 6);
    bcast.ifidx = WIFI_IF_STA;
    bcast.channel = 0;
    bcast.encrypt = false;
    if (esp_now_add_peer(&bcast) != ESP_OK) {
        Serial.println("[Link] add broadcast peer failed");
        return false;
    }

    load_peer_from_nvs();
    if (s_has_peer) {
        char macstr[18];
        mac_to_str(s_peer, macstr, sizeof(macstr));
        Serial.printf("[Link] stored peer %s ch=%u\n", macstr,
                      static_cast<unsigned>(s_stored_chan));
    }
    s_inited = true;
    start_search(JOY_LINK_SEARCH);
    return true;
}

void joy_link_update() {
    if (!s_inited) {
        return;
    }
    if (s_ack_pending) {
        if (s_state == JOY_LINK_LINKED) {
            s_ack_pending = false;  // リンク後の重複 ACK は捨てる
        } else {
            on_ack_received();
        }
        return;
    }
    if (s_relink_pending) {
        s_relink_pending = false;
        if (s_state != JOY_LINK_LINKED && s_has_peer) {
            // LOST 中に送り続けていた JOY が届いた → ACK を待たずに復帰する。
            const uint8_t chan = current_channel();
            if (chan != s_stored_chan && chan >= kChanMin && chan <= kChanMax) {
                s_stored_chan = chan;
                save_peer_to_nvs();
            }
            s_state = JOY_LINK_LINKED;
            s_phase = PHASE_DONE;
            s_stats.fail_streak = 0;
            s_last_keepalive_ms = millis();
            s_stats.relink_joy++;
            Serial.printf("[Link] relinked via joy ch=%u\n", static_cast<unsigned>(chan));
        }
        return;
    }
    if (s_state == JOY_LINK_LINKED) {
        // 端末 (CoreS3) が再起動するとペア情報 (RAM) を失うが、リモコンの JOY ユニキャストは
        // MAC レベルで届き続けるためリモコンは断を検知できず、端末は DISCOVER を受けない限り
        // 再ペアできない (デッドロック)。これを防ぐため LINKED 中も定期的に DISCOVER を
        // ブロードキャストする。端末は既知/未知どちらのピアの DISCOVER にも再 ACK するので、
        // 再起動を跨いで自動再リンクする。既にリンク済みの端末からの重複 ACK は捨てる。
        if (millis() - s_last_keepalive_ms >= JOY_KEEPALIVE_DISCOVER_MS) {
            s_last_keepalive_ms = millis();
            send_discover();
        }
        // 連続失敗 N 回だけでは落とさない (電波干渉で 0.5 s 程度の失敗は普通に起きる)。
        // 最後の成功から JOY_LINK_LOST_MS 経っても 1 回も届かないときだけ LOST にする。
        if (s_stats.fail_streak >= JOY_LINK_FAIL_N && millis() - s_last_ok_ms >= JOY_LINK_LOST_MS) {
            s_stats.lost_events++;
            Serial.printf("[Link] lost (fail_streak=%u, no delivery for %lu ms)\n",
                          static_cast<unsigned>(s_stats.fail_streak),
                          static_cast<unsigned long>(millis() - s_last_ok_ms));
            start_search(JOY_LINK_LOST);
        }
        return;
    }
    if (millis() - s_wait_start >= JOY_ACK_WAIT_MS) {
        advance_search();
    }
}

bool joy_link_send_joy(const joyp::Packet& p) {
    // LINKED でなくてもピアが分かっていれば送る (LOST 中の JOY は復帰の探針を兼ねる。
    // ホッピング中は別チャネルに投げるだけで害はない)。
    if (!s_inited || !s_has_peer) {
        return false;
    }
    uint8_t buf[joyp::kPacketLen];
    joyp::encode(p, buf);
    s_stats.tx_total++;
    if (esp_now_send(s_peer, buf, sizeof(buf)) != ESP_OK) {
        // 同期エラー (ピア未登録・キュー溢れ) も送達失敗として数える
        s_stats.tx_fail++;
        s_stats.fail_streak++;
        return false;
    }
    return true;
}

JoyLinkState joy_link_state() {
    return s_state;
}

const char* joy_link_state_name() {
    switch (s_state) {
        case JOY_LINK_LINKED:
            return "LINKED";
        case JOY_LINK_LOST:
            return "LOST";
        default:
            return "SEARCH";
    }
}

uint8_t joy_link_channel() {
    return s_chan;
}

uint8_t joy_link_radio_channel() {
    uint8_t primary = 0;
    wifi_second_chan_t second = WIFI_SECOND_CHAN_NONE;
    if (esp_wifi_get_channel(&primary, &second) != ESP_OK) {
        return 0;
    }
    return primary;
}

const uint8_t* joy_link_peer_mac() {
    return s_peer;
}

bool joy_link_has_peer() {
    return s_has_peer;
}

void joy_link_repair() {
    Serial.println("[Link] repair");
    clear_peer_in_nvs();
    if (s_has_peer) {
        esp_now_del_peer(s_peer);
    }
    memset(s_peer, 0, sizeof(s_peer));
    s_has_peer = false;
    s_stored_chan = 0;
    s_stats.tx_total = 0;
    s_stats.tx_ok = 0;
    s_stats.tx_fail = 0;
    start_search(JOY_LINK_SEARCH);
}

const JoyLinkStats& joy_link_stats() {
    return s_stats;
}
