// ---------------------------------------------------------------------------
// StackChanMove firmware — main loop
//
// stackchan_fsr の CoreS3 ファームウェアから派生。首制御 (pose) / 首振りジェスチャー /
// 顔追従を削除し、SCS0009 を左右車輪として駆動する drive_controller と、位置
// フィードバックからの odometry を追加した。音声・表情・口パク・設定 GUI は維持。
//
// Hub → 端末のテキストメッセージ (handle_text): drive / wheel_pwm /
// drive_source / odom_rate / odom_reset / camera / listen / speak_start / speak_end /
// anim / face。
// ---------------------------------------------------------------------------
#include <Arduino.h>
#include <ArduinoJson.h>
#include <M5Unified.h>
#include <esp_system.h>

#include "audio_capture.h"
#include "audio_playback.h"
#include "camera_streamer.h"
#include "config.h"
#include "config_store.h"
#include "diffdrive_math.h"
#include "drive_controller.h"
#include "face_controller.h"
#include "joystick.h"
#include "joystick_link.h"
#include "net_link.h"
#include "odometry.h"
#include "setup_gui.h"
#include "speak_watchdog.h"
#include "wheel_driver.h"
#include "wifi_manager.h"

// バイナリフレームは 5 バイトヘッダ [kind u8][ts_ms u32 LE] + payload
static constexpr size_t FRAME_HDR_LEN = 5;
static constexpr uint8_t FRAME_KIND_AUDIO = 0x01;

static String s_cfg_ssid;
static String s_cfg_pass;
static String s_cfg_hub;

static bool s_listening = false;
static bool s_mic_recording = false;

// 発話アニメーション (口パク) のスイッチ (Hub の "anim" スナップショット)。
// stackchan_fsr の首振り (旧 anim.gesture) は StackChanMove に首が無いため契約ごと削除した。
static bool s_anim_mouth = true;
static int16_t s_mic_buf[DMA_BUF_LEN];

// 発話の見張り。s_speaking は speak_start から speak_end / 見張りによる
// 停止までの間 true。s_last_speak_rx_ms は speak_start と音声フレームを受けた時刻。
static bool s_speaking = false;
static unsigned long s_last_speak_rx_ms = 0;
static uint32_t s_speak_timeout_count = 0;  // 音声が届かず止めた回数 (累計)
static uint32_t s_speak_abort_count = 0;    // 制御 WS の切断で止めた回数 (累計)

// マイク PCM は DMA ブロック (512 サンプル = 32 ms, 1 KB) ごとに 1 フレームで送る。
// stackchan_fsr の 1 秒 (31 KB) 一括送信は WS の同期送信で loop() を数百 ms 止め、
// drive の処理 (500 ms ウォッチドッグ) を破綻させるため。
// 送信バッファ先頭に 5 バイトのヘッダ領域を確保し、PCM はその後ろに直接置く。
static uint8_t s_mic_frame[FRAME_HDR_LEN + DMA_BUF_LEN * sizeof(int16_t)];

static unsigned long s_mic_frame_count = 0;
static int16_t s_peak_amplitude = 0;
static unsigned long s_last_diag_ms = 0;

static unsigned long s_touch_start_ms = 0;
static bool s_touch_active = false;

static bool s_ws_was_connected = false;
static bool s_wifi_was_connected = false;  // ESP-NOW 再初期化用の WiFi 立ち上がりエッジ
static bool s_boot_event_sent = false;
static unsigned long s_last_odom_send_ms = 0;
static uint32_t s_odom_seq = 0;
// odom 送信周期 [ms]。Hub の odom_rate メッセージで実行時に変更する (NVS 保存なし)。
static uint32_t s_odom_send_ms = 1000 / ODOM_SEND_HZ_DEFAULT;
static uint32_t s_odom_tx = 0;         // 診断期間中の odom 送信数 (実効レート確認用)
static uint32_t s_odom_stats_div = 0;  // wheel.stats は odom 10 回に 1 回だけ載せる

// loop() stall-source measurement (stackchan_fsr 由来)
static uint32_t s_max_m5 = 0;
static uint32_t s_max_wifi = 0;
static uint32_t s_max_ws = 0;
static uint32_t s_max_mic = 0;
static uint32_t s_max_anim = 0;
static uint32_t s_max_joy = 0;
static uint32_t s_max_drive = 0;
static uint32_t s_max_loop = 0;
static unsigned long s_prev_loop_ms = 0;

static inline void track_max(uint32_t* slot, unsigned long t0) {
    uint32_t d = static_cast<uint32_t>(millis() - t0);
    if (d > *slot) *slot = d;
}

static int16_t peak_amplitude(const int16_t* buf, size_t len) {
    int16_t peak = 0;
    for (size_t i = 0; i < len; ++i) {
        int16_t v = buf[i] < 0 ? -buf[i] : buf[i];
        if (v > peak) peak = v;
    }
    return peak;
}

static void write_frame_header(uint8_t* buf, uint8_t kind, uint32_t ts_ms) {
    buf[0] = kind;
    buf[1] = static_cast<uint8_t>(ts_ms & 0xFF);
    buf[2] = static_cast<uint8_t>((ts_ms >> 8) & 0xFF);
    buf[3] = static_cast<uint8_t>((ts_ms >> 16) & 0xFF);
    buf[4] = static_cast<uint8_t>((ts_ms >> 24) & 0xFF);
}

// ---------------------------------------------------------------------------
// Outgoing messages
// ---------------------------------------------------------------------------
static void send_event(const char* name, const char* detail) {
    JsonDocument doc;
    doc["type"] = "event";
    doc["name"] = name;
    doc["detail"] = detail;
    char buf[256];
    serializeJson(doc, buf, sizeof(buf));
    net_send_text(buf, false);
}

static void send_boot_event() {
    char detail[96];
    snprintf(detail, sizeof(detail), "reset=%d;wheel_d=%.1f;track=%.1f;wheel_ok=%d",
             static_cast<int>(esp_reset_reason()), static_cast<double>(WHEEL_DIAMETER_MM),
             static_cast<double>(TRACK_WIDTH_MM), wheel_driver_ok() ? 1 : 0);
    send_event("boot", detail);
}

static void send_odom() {
    OdomSnapshot s;
    odometry_take_snapshot(&s);
    DriveState d;
    drive_controller_get_state(&d);

    JsonDocument doc;
    doc["type"] = "odom";
    doc["ts"] = s.sample_ms;  // 送信時刻ではなく姿勢のサンプル時刻
    doc["seq"] = s_odom_seq++;
    doc["x"] = s.x;
    doc["y"] = s.y;
    doc["th"] = s.th;
    doc["v"] = s.v;
    doc["w"] = s.w;
    doc["dl"] = s.dl;
    doc["dr"] = s.dr;
    JsonObject cmd = doc["cmd"].to<JsonObject>();
    cmd["v"] = d.v;
    cmd["w"] = d.w;
    cmd["src"] = drive_source_str(d.src);
    JsonObject wheel = doc["wheel"].to<JsonObject>();
    wheel["l_pos"] = s.l_pos;
    wheel["r_pos"] = s.r_pos;
    wheel["l_valid"] = s.l_valid;
    wheel["r_valid"] = s.r_valid;
    // トラッカ状態と Δθ の出所 (後方互換の追加キー)
    wheel["l_state"] = ddm::pos_state_str(static_cast<ddm::PosState>(s.l_state));
    wheel["r_state"] = ddm::pos_state_str(static_cast<ddm::PosState>(s.r_state));
    wheel["l_src"] = ddm::pos_src_str(static_cast<ddm::PosSrc>(s.l_src));
    wheel["r_src"] = ddm::pos_src_str(static_cast<ddm::PosSrc>(s.r_src));
    if ((s_odom_stats_div++ % 10) == 0) {
        JsonObject st = wheel["stats"].to<JsonObject>();
        JsonObject sl = st["l"].to<JsonObject>();
        sl["cross"] = s.l_cross;
        sl["lost"] = s.l_lost;
        sl["reject"] = s.l_reject;
        sl["extrap_rad"] = s.l_extrap_rad;
        JsonObject sr = st["r"].to<JsonObject>();
        sr["cross"] = s.r_cross;
        sr["lost"] = s.r_lost;
        sr["reject"] = s.r_reject;
        sr["extrap_rad"] = s.r_extrap_rad;
    }

    char buf[640];
    serializeJson(doc, buf, sizeof(buf));
    net_send_text(buf, true);  // odom は latest-wins (送信が詰まれば古いものを捨てる)
    ++s_odom_tx;
}

// ---------------------------------------------------------------------------
// Speak
// ---------------------------------------------------------------------------
// speak_end を受けたとき・見張りで止めるとき: リングを鳴らし切って止め、口パクを止める。
static void finish_speak(const char* tag) {
    s_speaking = false;
    audio_playback_diag_report(tag);
    audio_playback_stop();
    face_speak_anim_stop();
}

// 制御 WS が切れたとき: リングに残った音を捨てて即座に止め、口パクを止める。
static void abort_speak() {
    if (!s_speaking) return;
    s_speaking = false;
    ++s_speak_abort_count;
    Serial.println("[Main] Speak aborted (WS disconnected)");
    audio_playback_diag_report("abort");
    audio_playback_abort();
    face_speak_anim_stop();
}

// 発話中に音声フレームが SPEAK_RX_TIMEOUT_MS 届かなければ speak_end と同じ処理をする。
static void check_speak_timeout() {
    const uint32_t now = static_cast<uint32_t>(millis());
    const uint32_t last = static_cast<uint32_t>(s_last_speak_rx_ms);
    if (!spkwd::should_stop(s_speaking, last, now, SPEAK_RX_TIMEOUT_MS)) return;
    ++s_speak_timeout_count;
    Serial.printf("[Main] Speak timeout (no audio for %lu ms)\n",
                  static_cast<unsigned long>(spkwd::elapsed_ms(last, now)));
    finish_speak("speak_timeout");
}

// ---------------------------------------------------------------------------
// Incoming messages
// ---------------------------------------------------------------------------
static void handle_text(const char* data, size_t len) {
    JsonDocument doc;
    DeserializationError err = deserializeJson(doc, data, len);
    if (err) {
        Serial.printf("[Main] JSON parse error: %s\n", err.c_str());
        return;
    }

    const char* type = doc["type"];
    if (!type) return;

    if (strcmp(type, "drive") == 0) {
        float v = doc["v"] | 0.0f;
        float w = doc["w"] | 0.0f;
        uint32_t seq = doc["seq"] | 0u;
        const char* src = doc["src"] | "hub";
        drive_controller_set_hub_cmd(v, w, seq, src);
    } else if (strcmp(type, "wheel_pwm") == 0) {
        // 校正用の生 PWM。受理条件 (drive_source == hub) と
        // クランプ・ウォッチドッグは drive_controller 側で扱う。
        int l = doc["l"] | 0;
        int r = doc["r"] | 0;
        uint32_t seq = doc["seq"] | 0u;
        drive_controller_set_hub_pwm(l, r, seq);
    } else if (strcmp(type, "drive_source") == 0) {
        const char* source = doc["source"] | "hub";
        drive_controller_set_source(source);
    } else if (strcmp(type, "odom_rate") == 0) {
        // Hub が接続ごとに送る odom 送信レート。NVS には保存しない。
        int hz = doc["hz"] | ODOM_SEND_HZ_DEFAULT;
        if (hz < ODOM_SEND_HZ_MIN) hz = ODOM_SEND_HZ_MIN;
        if (hz > ODOM_SEND_HZ_MAX) hz = ODOM_SEND_HZ_MAX;
        s_odom_send_ms = static_cast<uint32_t>(1000 / hz);
        Serial.printf("[Main] odom_rate %d Hz\n", hz);
    } else if (strcmp(type, "odom_reset") == 0) {
        Serial.println("[Main] odom_reset");
        odometry_reset();
    } else if (strcmp(type, "camera") == 0) {
        camera_streamer_set_config(doc["on"] | true, doc["fps"] | CAM_STREAM_FPS,
                                   doc["quality"] | CAM_JPEG_QUALITY);
    } else if (strcmp(type, "listen") == 0) {
        bool on = doc["on"] | false;
        if (on) {
            Serial.println("[Main] Listen ON");
            s_listening = true;
            audio_capture_start();
            audio_capture_read(s_mic_buf, DMA_BUF_LEN);
            s_mic_recording = true;
        } else {
            Serial.println("[Main] Listen OFF");
            s_listening = false;
            s_mic_recording = false;
            audio_capture_stop();
        }
    } else if (strcmp(type, "speak_start") == 0) {
        Serial.println("[Main] Speak start");
        s_listening = false;
        s_mic_recording = false;
        audio_capture_stop();
        audio_playback_start();
        if (s_anim_mouth) face_speak_anim_start();
        s_speaking = true;
        s_last_speak_rx_ms = millis();
    } else if (strcmp(type, "speak_end") == 0) {
        Serial.println("[Main] Speak end");
        finish_speak("speak_end");
    } else if (strcmp(type, "anim") == 0) {
        bool mouth = doc["mouth"] | s_anim_mouth;
        Serial.printf("[Main] Anim mouth=%d\n", mouth);
        if (audio_playback_is_active() && mouth != s_anim_mouth) {
            if (mouth) {
                face_speak_anim_start();
            } else {
                face_speak_anim_stop();
            }
        }
        s_anim_mouth = mouth;
    } else if (strcmp(type, "face") == 0) {
        const char* expr = doc["expr"] | "neutral";
        face_set_expression(expr);
    } else {
        Serial.printf("[Main] Unknown message type: %s (ignored)\n", type);
    }
}

static void handle_binary(const uint8_t* data, size_t len) {
    if (len < FRAME_HDR_LEN) {
        Serial.printf("[Main] Binary frame too short (%u)\n", static_cast<unsigned>(len));
        return;
    }
    uint8_t kind = data[0];
    if (kind != FRAME_KIND_AUDIO) {
        Serial.printf("[Main] Binary kind 0x%02X dropped\n", kind);
        return;
    }
    s_last_speak_rx_ms = millis();
    if (audio_playback_is_active()) {
        audio_playback_feed(data + FRAME_HDR_LEN, len - FRAME_HDR_LEN);
    }
}

static void stop_audio() {
    s_listening = false;
    s_mic_recording = false;
    audio_capture_stop();
    s_speaking = false;
    audio_playback_stop();
}

static void load_config_from_nvs() {
    s_cfg_ssid = config_store_get_ssid();
    s_cfg_pass = config_store_get_password();
    s_cfg_hub = config_store_get_hub_url();
}

static void run_setup_and_update_config() {
    SetupResult result = setup_gui_run();
    if (result.completed) {
        s_cfg_ssid = result.ssid;
        s_cfg_pass = result.password;
        s_cfg_hub = result.hub_url;
    }
}

// ---------------------------------------------------------------------------
// setup / loop
// ---------------------------------------------------------------------------
void setup() {
    // BSP の M5StackChan.begin() は使わない (Motion タスクがサーボバスを占有するため)。
    auto cfg = M5.config();
    M5.begin(cfg);
    Serial.begin(115200);
    Serial.printf("[Diag] reset_reason=%d\n", static_cast<int>(esp_reset_reason()));
    Serial.println("[Main] StackChanMove firmware starting...");

    M5.Speaker.setVolume(200);
    config_store_init();

    bool need_setup = false;
    bool wifi_ok = false;
    if (config_store_has_config()) {
        load_config_from_nvs();
        wifi_ok = wifi_init(s_cfg_ssid.c_str(), s_cfg_pass.c_str());
        if (!wifi_ok) {
            need_setup = true;
        }
    } else {
        need_setup = true;
    }

    if (need_setup) {
        run_setup_and_update_config();
        wifi_ok = wifi_init(s_cfg_ssid.c_str(), s_cfg_pass.c_str());
    }

    face_init();
    camera_streamer_init();

    wheel_driver_init();
    odometry_init();
    drive_controller_init();

    // ESP-NOW は WiFi STA が上がってから初期化する。
    joystick_init();
    if (wifi_ok) joystick_link_init();
    s_wifi_was_connected = (WiFi.status() == WL_CONNECTED);

    // ネットワークは net_ctrl / net_media タスクが担い、loop() はキュー経由。
    net_init(s_cfg_hub.c_str());
}

void loop() {
    unsigned long loop_start = millis();
    if (s_prev_loop_ms != 0) {
        uint32_t interval = static_cast<uint32_t>(loop_start - s_prev_loop_ms);
        if (interval > s_max_loop) s_max_loop = interval;
    }
    s_prev_loop_ms = loop_start;

    unsigned long t0 = millis();
    M5.update();
    track_max(&s_max_m5, t0);

    auto touch = M5.Touch.getDetail();
    if (touch.isPressed()) {
        if (!s_touch_active) {
            s_touch_active = true;
            s_touch_start_ms = millis();
        } else if (millis() - s_touch_start_ms >= SETUP_LONG_PRESS_MS) {
            Serial.println("[Main] Long press detected — entering setup");
            s_touch_active = false;
            drive_controller_on_disconnect();
            camera_streamer_stop();
            stop_audio();
            face_suspend();
            run_setup_and_update_config();
            face_resume();
            wifi_init(s_cfg_ssid.c_str(), s_cfg_pass.c_str());
            s_wifi_was_connected = (WiFi.status() == WL_CONNECTED);
            if (s_wifi_was_connected) joystick_link_init();
            net_set_hub_url(s_cfg_hub.c_str());
            return;
        }
    } else {
        s_touch_active = false;
    }

    t0 = millis();
    wifi_ensure_connected();
    {
        // WiFi 再接続で STA チャネルが変わりうるため ESP-NOW を張り直す。
        const bool wifi_now = (WiFi.status() == WL_CONNECTED);
        if (wifi_now && !s_wifi_was_connected) joystick_link_init();
        s_wifi_was_connected = wifi_now;
    }
    track_max(&s_max_wifi, t0);

    // 受信キューを掃く (net_ctrl が積んだテキスト / スピーカー PCM)。ブロックしない。
    t0 = millis();
    {
        NetRxMsg m;
        while (net_rx_pop(&m)) {
            if (m.binary) {
                handle_binary(m.data, m.len);
            } else {
                handle_text(reinterpret_cast<const char*>(m.data), m.len);
            }
            net_rx_free(&m);
        }
    }
    track_max(&s_max_ws, t0);

    bool connected = net_ctrl_connected();
    if (connected && !s_ws_was_connected) {
        Serial.println("[Main] WS connected");
        if (!s_boot_event_sent) {
            send_boot_event();
            s_boot_event_sent = true;
        }
        camera_streamer_start();
    } else if (!connected && s_ws_was_connected) {
        Serial.println("[Main] WS disconnected — stopping drive and mic capture");
        drive_controller_on_disconnect();
        camera_streamer_stop();
        s_listening = false;
        s_mic_recording = false;
        audio_capture_stop();
        abort_speak();
    }
    s_ws_was_connected = connected;
    check_speak_timeout();

    t0 = millis();
    if (s_listening && s_mic_recording) {
        if (M5.Mic.isRecording() == 0) {
            memcpy(&s_mic_frame[FRAME_HDR_LEN], s_mic_buf, DMA_BUF_LEN * sizeof(int16_t));
            ++s_mic_frame_count;
            int16_t p = peak_amplitude(s_mic_buf, DMA_BUF_LEN);
            if (p > s_peak_amplitude) s_peak_amplitude = p;
            // 次のブロックの録音を先に開始してから送信する (送信中も録音を止めない)。
            audio_capture_read(s_mic_buf, DMA_BUF_LEN);
            write_frame_header(s_mic_frame, FRAME_KIND_AUDIO, static_cast<uint32_t>(millis()));
            net_mic_push(s_mic_frame, sizeof(s_mic_frame));
        }
    }
    track_max(&s_max_mic, t0);

    t0 = millis();
    face_speak_anim_update();
    track_max(&s_max_anim, t0);

    t0 = millis();
    joystick_update(connected);
    if (connected) {
        char joy_msg[192];
        while (joystick_pop_outgoing(joy_msg, sizeof(joy_msg))) {
            net_send_text(joy_msg, false);
        }
    }
    track_max(&s_max_joy, t0);

    t0 = millis();
    drive_controller_update();
    if (connected) {
        if (drive_controller_timeout_event_pending()) {
            send_event("drive_timeout", "no drive within DRIVE_TIMEOUT_MS");
            drive_controller_clear_timeout_event();
        }
        unsigned long now = millis();
        if (now - s_last_odom_send_ms >= s_odom_send_ms) {
            s_last_odom_send_ms = now;
            send_odom();
        }
    }
    track_max(&s_max_drive, t0);

    unsigned long now = millis();
    if (now - s_last_diag_ms >= DIAG_INTERVAL_MS) {
        // 診断期間の実測秒数 (odom 送信の実効レート算出用。loop() 周期に依存する)
        double diag_elapsed_s = static_cast<double>(now - s_last_diag_ms) / 1000.0;
        if (diag_elapsed_s <= 0.0) diag_elapsed_s = 1.0;
        Serial.printf(
            "[Diag] reset_reason=%d heap=%u/%u mic_frames=%lu peak=%d speak_timeout=%lu "
            "speak_abort=%lu\n",
            static_cast<int>(esp_reset_reason()), ESP.getFreeHeap(), ESP.getMinFreeHeap(),
            s_mic_frame_count, s_peak_amplitude, static_cast<unsigned long>(s_speak_timeout_count),
            static_cast<unsigned long>(s_speak_abort_count));
        Serial.printf(
            "[Diag] stall_max(ms): loop=%lu m5=%lu wifi=%lu rx=%lu mic=%lu anim=%lu joy=%lu "
            "drive=%lu\n",
            static_cast<unsigned long>(s_max_loop), static_cast<unsigned long>(s_max_m5),
            static_cast<unsigned long>(s_max_wifi), static_cast<unsigned long>(s_max_ws),
            static_cast<unsigned long>(s_max_mic), static_cast<unsigned long>(s_max_anim),
            static_cast<unsigned long>(s_max_joy), static_cast<unsigned long>(s_max_drive));
        net_diag_report();
        {
            OdomSnapshot o;
            odometry_peek(&o);
            DriveState d;
            drive_controller_get_state(&d);
            Serial.printf(
                "[Drive] src=%s sel=%s cmd=(%.2f,%.2f) pwm=(%d,%d) pos=(%d%c,%d%c) spd=(%d,%d) "
                "odom=(%.3f,%.3f,%.1fdeg) v=%.3f w=%.2f samples=%lu fail=%lu odom_tx=%.1fHz "
                "vbat=%.2f\n",
                drive_source_str(d.src), drive_source_str(d.source), static_cast<double>(d.v),
                static_cast<double>(d.w), d.pwm_l, d.pwm_r, o.l_pos,
                ddm::pos_state_char(static_cast<ddm::PosState>(o.l_state)), o.r_pos,
                ddm::pos_state_char(static_cast<ddm::PosState>(o.r_state)), o.l_spd, o.r_spd,
                static_cast<double>(o.x), static_cast<double>(o.y),
                static_cast<double>(o.th * 180.0f / 3.14159265f), static_cast<double>(o.v),
                static_cast<double>(o.w), static_cast<unsigned long>(o.samples),
                static_cast<unsigned long>(o.read_fail),
                static_cast<double>(s_odom_tx) / diag_elapsed_s,
                M5.Power.getBatteryVoltage() / 1000.0);
        }
        joystick_diag_report();
        camera_streamer_diag_report();
        // サーボ初期化状態 (USB CDC が起動直後の [Wheel] ログを落とすため定期的に出す)。
        // 失敗していれば再初期化を試みる (電源の後入れ・接触不良からの復帰用)。
        if (!wheel_driver_ok()) {
            Serial.printf("[Wheel] NOT ready (reason=%s) — retrying init\n",
                          wheel_driver_status_str());
            if (wheel_driver_init()) {
                Serial.println("[Wheel] init recovered");
                odometry_reset();
            }
        } else {
            Serial.printf("[Wheel] ready (%s)\n", wheel_driver_status_str());
        }
        if (audio_playback_is_active()) {
            audio_playback_diag_report("live");
        }
        s_max_loop = s_max_m5 = s_max_wifi = s_max_ws = 0;
        s_max_mic = s_max_anim = s_max_joy = s_max_drive = 0;
        s_mic_frame_count = 0;
        s_peak_amplitude = 0;
        s_odom_tx = 0;
        s_last_diag_ms = now;
    }

    delay(1);
}
