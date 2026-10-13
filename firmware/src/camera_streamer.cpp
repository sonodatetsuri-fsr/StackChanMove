#include "camera_streamer.h"

#include <Arduino.h>
#include <esp_heap_caps.h>
#include <esp_timer.h>
#include <freertos/FreeRTOS.h>
#include <freertos/semphr.h>
#include <freertos/task.h>
#include <stdlib.h>
#include <string.h>

#include "config.h"
#include "esp_camera.h"
#include "img_converters.h"

// ---------------------------------------------------------------------------
// 状態
// ---------------------------------------------------------------------------
static constexpr uint8_t kFrameKindJpeg = 0x02;
static constexpr uint32_t kIdleDelayMs = 50;     // 待機中のポーリング周期
static constexpr uint32_t kFbFailDelayMs = 100;  // fb_get 失敗時の待ち

static bool s_ready = false;

// loop() (単一の書き手) から更新される設定。int/bool の単一ワード更新のためロック不要。
static volatile bool s_running = false;  // WS 接続中か
static volatile bool s_on = true;        // Hub の camera.on
static volatile int s_fps = CAM_STREAM_FPS;
static volatile int s_quality = CAM_JPEG_QUALITY;

// latest-wins スロット (ミューテックス保護)
static SemaphoreHandle_t s_mutex = nullptr;
static uint8_t* s_slot = nullptr;
static size_t s_slot_len = 0;

// 診断カウンタ
static volatile uint32_t s_encoded = 0;      // 前回診断からの JPEG 化枚数
static volatile uint32_t s_last_jpeg = 0;    // 直近の JPEG バイト数
static volatile uint32_t s_flip_max_us = 0;  // 診断期間中の向き補正 (flip_rgb565) の最大時間
static volatile uint32_t s_jpeg_max_us = 0;  // 同 frame2jpg の最大時間
static volatile uint32_t s_drop = 0;         // 上書きで捨てたフレームの累積数
// フレーム ts_ms (fb->timestamp) と millis() の差。
// 0 以上かつ要求周期未満なら fb->timestamp は millis() と同じ esp_timer 基準。負値や数百 ms
// なら基準が異なるので ts_ms を millis() に戻すこと。
static volatile int32_t s_ts_lag_min = 0;
static volatile int32_t s_ts_lag_max = 0;
static volatile bool s_ts_lag_seen = false;
static unsigned long s_last_report_ms = 0;
static unsigned long s_last_fail_log_ms = 0;

static inline void cam_lock() {
    if (s_mutex) xSemaphoreTake(s_mutex, portMAX_DELAY);
}

static inline void cam_unlock() {
    if (s_mutex) xSemaphoreGive(s_mutex);
}

/// スロットを空にする。呼び出し側でロックを保持していること。
static void discard_slot_locked() {
    if (s_slot) {
        free(s_slot);
        s_slot = nullptr;
        s_slot_len = 0;
    }
}

static int clamp_int(int v, int lo, int hi) {
    if (v < lo) return lo;
    if (v > hi) return hi;
    return v;
}

// ---------------------------------------------------------------------------
// 向き補正。センサの set_hmirror/set_vflip は GC0308 で
// 効くか・既定がどちらかをソースで確認できない (esp32-camera はプリコンパイル) ため、
// RGB565 フレームをソフトウェアで反転する。QVGA で数 ms、Core 0 で行う。
// ---------------------------------------------------------------------------
static void flip_rgb565(camera_fb_t* fb, bool hmirror, bool vflip) {
    if (!fb || fb->format != PIXFORMAT_RGB565 || (!hmirror && !vflip)) return;
    const size_t w = fb->width;
    const size_t h = fb->height;
    uint16_t* px = reinterpret_cast<uint16_t*>(fb->buf);
    if (hmirror) {
        for (size_t y = 0; y < h; ++y) {
            uint16_t* row = px + y * w;
            for (size_t l = 0, r = w - 1; l < r; ++l, --r) {
                const uint16_t t = row[l];
                row[l] = row[r];
                row[r] = t;
            }
        }
    }
    if (vflip) {
        for (size_t top = 0, bot = h - 1; top < bot; ++top, --bot) {
            uint16_t* a = px + top * w;
            uint16_t* b = px + bot * w;
            for (size_t x = 0; x < w; ++x) {
                const uint16_t t = a[x];
                a[x] = b[x];
                b[x] = t;
            }
        }
    }
}

// ---------------------------------------------------------------------------
// 取得・変換タスク (Core 0, 優先度 1)
// ---------------------------------------------------------------------------
static void camera_streamer_task(void* arg) {
    (void)arg;
    for (;;) {
        if (!(s_ready && s_running && s_on)) {
            vTaskDelay(pdMS_TO_TICKS(kIdleDelayMs));
            continue;
        }

        unsigned long t0 = millis();

        camera_fb_t* fb = esp_camera_fb_get();
        if (fb == nullptr) {
            if (millis() - s_last_fail_log_ms >= DIAG_INTERVAL_MS) {
                s_last_fail_log_ms = millis();
                Serial.println("[Cam] fb_get failed");
            }
            vTaskDelay(pdMS_TO_TICKS(kFbFailDelayMs));
            continue;
        }
        // 撮像時刻 = フレーム先頭 DMA バッファの時刻 (esp_camera.h: 起動からの時刻、millis() と
        // 同じ esp_timer 基準)。0 のドライバでは millis() にフォールバックする。
        uint32_t ts_ms;
        if (fb->timestamp.tv_sec == 0 && fb->timestamp.tv_usec == 0) {
            ts_ms = static_cast<uint32_t>(millis());
        } else {
            ts_ms = static_cast<uint32_t>(fb->timestamp.tv_sec) * 1000u +
                    static_cast<uint32_t>(fb->timestamp.tv_usec / 1000);
        }
        {
            const int32_t lag = static_cast<int32_t>(static_cast<uint32_t>(millis()) - ts_ms);
            if (!s_ts_lag_seen) {
                s_ts_lag_seen = true;
                s_ts_lag_min = lag;
                s_ts_lag_max = lag;
            } else {
                if (lag < s_ts_lag_min) s_ts_lag_min = lag;
                if (lag > s_ts_lag_max) s_ts_lag_max = lag;
            }
        }
        const int64_t t_flip = esp_timer_get_time();
        flip_rgb565(fb, CAM_HMIRROR != 0, CAM_VFLIP != 0);
        const int64_t t_jpg = esp_timer_get_time();
        const uint32_t flip_us = static_cast<uint32_t>(t_jpg - t_flip);
        if (flip_us > s_flip_max_us) s_flip_max_us = flip_us;

        uint8_t* jpg = nullptr;
        size_t jpg_len = 0;
        bool ok = frame2jpg(fb, static_cast<uint8_t>(s_quality), &jpg, &jpg_len);
        const uint32_t jpg_us = static_cast<uint32_t>(esp_timer_get_time() - t_jpg);
        if (jpg_us > s_jpeg_max_us) s_jpeg_max_us = jpg_us;
        esp_camera_fb_return(fb);
        if (!ok || jpg == nullptr || jpg_len == 0) {
            if (jpg) free(jpg);
            continue;
        }

        // 5 バイトヘッダ込みで確保する (loop() はそのまま送るだけ)。
        size_t total = CAM_FRAME_HDR_LEN + jpg_len;
        uint8_t* out =
            static_cast<uint8_t*>(heap_caps_malloc(total, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT));
        if (out == nullptr) out = static_cast<uint8_t*>(malloc(total));
        if (out == nullptr) {
            free(jpg);
            continue;
        }
        out[0] = kFrameKindJpeg;
        out[1] = static_cast<uint8_t>(ts_ms & 0xFF);
        out[2] = static_cast<uint8_t>((ts_ms >> 8) & 0xFF);
        out[3] = static_cast<uint8_t>((ts_ms >> 16) & 0xFF);
        out[4] = static_cast<uint8_t>((ts_ms >> 24) & 0xFF);
        memcpy(out + CAM_FRAME_HDR_LEN, jpg, jpg_len);
        free(jpg);

        cam_lock();
        if (s_slot) {  // 未送信フレームは捨てる (latest-wins)
            free(s_slot);
            ++s_drop;
        }
        s_slot = out;
        s_slot_len = total;
        cam_unlock();

        ++s_encoded;
        s_last_jpeg = static_cast<uint32_t>(jpg_len);

        int fps = s_fps;
        if (fps < CAM_FPS_MIN) fps = CAM_FPS_MIN;
        uint32_t period = static_cast<uint32_t>(1000 / fps);
        uint32_t spent = static_cast<uint32_t>(millis() - t0);
        TickType_t delay_ticks = (spent >= period) ? 1 : pdMS_TO_TICKS(period - spent);
        if (delay_ticks < 1) delay_ticks = 1;
        vTaskDelay(delay_ticks);
    }
}

// ---------------------------------------------------------------------------
// 公開 API
// ---------------------------------------------------------------------------
bool camera_streamer_init() {
    if (s_ready) return true;

    camera_config_t cfg = {};
    cfg.pin_pwdn = -1;
    cfg.pin_reset = -1;
    cfg.pin_xclk = -1;
    cfg.pin_sccb_sda = -1;
    cfg.pin_sccb_scl = -1;
    cfg.sccb_i2c_port = 1;
    cfg.pin_d7 = 47;
    cfg.pin_d6 = 48;
    cfg.pin_d5 = 16;
    cfg.pin_d4 = 15;
    cfg.pin_d3 = 42;
    cfg.pin_d2 = 41;
    cfg.pin_d1 = 40;
    cfg.pin_d0 = 39;
    cfg.pin_vsync = 46;
    cfg.pin_href = 38;
    cfg.pin_pclk = 45;
    cfg.xclk_freq_hz = 20000000;
    cfg.ledc_timer = LEDC_TIMER_0;
    cfg.ledc_channel = LEDC_CHANNEL_0;
    cfg.pixel_format = PIXFORMAT_RGB565;
    cfg.frame_size = FRAMESIZE_QVGA;
    cfg.jpeg_quality = 0;
    cfg.fb_count = 1;
    cfg.fb_location = CAMERA_FB_IN_PSRAM;
    cfg.grab_mode = CAMERA_GRAB_WHEN_EMPTY;

    esp_err_t err = esp_camera_init(&cfg);
    if (err != ESP_OK) {
        Serial.printf("[Cam] init failed (0x%x)\n", static_cast<unsigned>(err));
        return false;
    }

    // 向き: 非鏡像を正とし、フレームをソフトウェアで反転する
    // (センサ設定には触らない)。センサがドライバ既定で何と報告しているかは記録だけする。
    {
        sensor_t* sensor = esp_camera_sensor_get();
        Serial.printf(
            "[Cam] orientation sw hmirror=%d vflip=%d (sensor default hmirror=%d vflip=%d)\n",
            CAM_HMIRROR, CAM_VFLIP, sensor ? sensor->status.hmirror : -1,
            sensor ? sensor->status.vflip : -1);
    }

    if (s_mutex == nullptr) s_mutex = xSemaphoreCreateMutex();
    s_ready = true;
    s_last_report_ms = millis();
    xTaskCreatePinnedToCore(camera_streamer_task, "cam", CAM_TASK_STACK, nullptr, CAM_TASK_PRIO,
                            nullptr, CAM_TASK_CORE);
    Serial.println("[Cam] init ok QVGA");
    return true;
}

bool camera_streamer_is_ready() {
    return s_ready;
}

void camera_streamer_start() {
    s_running = true;
}

void camera_streamer_stop() {
    s_running = false;
    cam_lock();
    discard_slot_locked();
    cam_unlock();
}

void camera_streamer_set_config(bool on, int fps, int quality) {
    s_on = on;
    s_fps = clamp_int(fps, CAM_FPS_MIN, CAM_FPS_MAX);
    s_quality = clamp_int(quality, CAM_QUALITY_MIN, CAM_QUALITY_MAX);
    Serial.printf("[Cam] config on=%d fps=%d quality=%d\n", static_cast<int>(on), s_fps, s_quality);
    if (!on) {
        cam_lock();
        discard_slot_locked();
        cam_unlock();
    }
}

bool camera_streamer_take(uint8_t** frame, size_t* len) {
    if (frame == nullptr || len == nullptr) return false;
    cam_lock();
    uint8_t* p = s_slot;
    size_t n = s_slot_len;
    s_slot = nullptr;
    s_slot_len = 0;
    cam_unlock();
    if (p == nullptr) return false;
    *frame = p;
    *len = n;
    return true;
}

void camera_streamer_release(uint8_t* frame) {
    if (frame) free(frame);
}

void camera_streamer_diag_report() {
    if (!s_ready) return;
    unsigned long now = millis();
    float elapsed_s = static_cast<float>(now - s_last_report_ms) / 1000.0f;
    if (elapsed_s <= 0.0f) elapsed_s = 1.0f;
    float fps = static_cast<float>(s_encoded) / elapsed_s;
    Serial.printf(
        "[Cam] fps=%.1f jpeg=%uB drop=%u flip_max=%.1fms jpeg_max=%.1fms ts_lag=%ld..%ldms\n",
        static_cast<double>(fps), static_cast<unsigned>(s_last_jpeg), static_cast<unsigned>(s_drop),
        static_cast<double>(s_flip_max_us) / 1000.0, static_cast<double>(s_jpeg_max_us) / 1000.0,
        static_cast<long>(s_ts_lag_min), static_cast<long>(s_ts_lag_max));
    s_flip_max_us = 0;
    s_jpeg_max_us = 0;
    s_ts_lag_seen = false;
    s_ts_lag_min = 0;
    s_ts_lag_max = 0;
    s_encoded = 0;
    s_last_report_ms = now;
}
