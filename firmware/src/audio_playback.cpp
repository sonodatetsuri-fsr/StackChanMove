#include "audio_playback.h"

#include <M5Unified.h>
#include <esp_heap_caps.h>
#include <string.h>

#include <atomic>

#include "config.h"

// ---------------------------------------------------------------------------
// Ring buffer (PSRAM-allocated for large TTS audio)
//
// SPSC across tasks: producer = WebSocket binary callback running in
// the Arduino loop task (audio_playback_feed), consumer = the dedicated feed
// task below. Each side writes only its own position; positions are atomics
// with release stores / acquire loads so payload bytes are visible before the
// position advances.
// ---------------------------------------------------------------------------

static uint8_t* s_ring = nullptr;
static size_t s_ring_size = 0;
static std::atomic<size_t> s_write_pos{0};
static std::atomic<size_t> s_read_pos{0};

static void ring_init() {
    if (s_ring) return;
    s_ring = (uint8_t*)heap_caps_malloc(PLAYBACK_BUF_SIZE, MALLOC_CAP_SPIRAM);
    if (!s_ring) {
        s_ring = (uint8_t*)malloc(PLAYBACK_BUF_SIZE);
    }
    s_ring_size = s_ring ? PLAYBACK_BUF_SIZE : 0;
    Serial.printf("[Playback] Ring buffer: %u bytes on %s\n", s_ring_size,
                  heap_caps_get_free_size(MALLOC_CAP_SPIRAM) > 0 ? "PSRAM" : "heap");
}

static void ring_clear() {
    s_write_pos.store(0, std::memory_order_release);
    s_read_pos.store(0, std::memory_order_release);
}

static size_t ring_available() {
    size_t w = s_write_pos.load(std::memory_order_acquire);
    size_t r = s_read_pos.load(std::memory_order_acquire);
    return (w - r + s_ring_size) % s_ring_size;
}

static size_t ring_free() {
    return s_ring_size - 1 - ring_available();
}

// ---------------------------------------------------------------------------
// Diagnostics counters (measure real playback behaviour)
// ---------------------------------------------------------------------------

static uint32_t s_diag_overflow_bytes = 0;  // bytes dropped by ring_write truncation
static uint32_t s_diag_underrun_count = 0;  // audible gap events after streaming started
static uint32_t s_diag_feed_count = 0;      // chunks handed to playRaw
static size_t s_diag_max_occupancy = 0;     // peak ring_available (bytes)
static bool s_diag_misaligned = false;      // write_pos landed on odd byte (int16 boundary lost)

static void ring_diag_reset() {
    s_diag_overflow_bytes = 0;
    s_diag_underrun_count = 0;
    s_diag_feed_count = 0;
    s_diag_max_occupancy = 0;
    s_diag_misaligned = false;
}

static size_t ring_write(const uint8_t* data, size_t len) {
    if (!s_ring) return 0;
    size_t avail = ring_free();
    if (len > avail) {
        // Truncate on an int16 boundary so a full ring can never shift the
        // sample alignment of everything that follows (noise).
        size_t truncated = avail & ~static_cast<size_t>(1);
        s_diag_overflow_bytes += (len - truncated);  // dropped bytes = overflow
        len = truncated;
    }
    size_t wp = s_write_pos.load(std::memory_order_relaxed);
    size_t first = s_ring_size - wp;
    if (first > len) first = len;
    memcpy(&s_ring[wp], data, first);
    if (len > first) {
        memcpy(&s_ring[0], data + first, len - first);
    }
    size_t new_wp = (wp + len) % s_ring_size;
    s_write_pos.store(new_wp, std::memory_order_release);
    if (new_wp & 1) s_diag_misaligned = true;  // odd write_pos => reads misalign => noise
    size_t occ = ring_available();
    if (occ > s_diag_max_occupancy) s_diag_max_occupancy = occ;
    return len;
}

static size_t ring_read(uint8_t* dst, size_t len) {
    if (!s_ring) return 0;
    size_t avail = ring_available();
    if (len > avail) len = avail;
    size_t rp = s_read_pos.load(std::memory_order_relaxed);
    size_t first = s_ring_size - rp;
    if (first > len) first = len;
    memcpy(dst, &s_ring[rp], first);
    if (len > first) {
        memcpy(dst + first, &s_ring[0], len - first);
    }
    s_read_pos.store((rp + len) % s_ring_size, std::memory_order_release);
    return len;
}

// ---------------------------------------------------------------------------
// Playback state
// ---------------------------------------------------------------------------

static std::atomic<bool> s_playing{false};
static std::atomic<bool> s_stop_requested{false};
static std::atomic<bool> s_abort_requested{false};  // リングを捨てて即停止

/// Chunk size in samples fed to playRaw at a time. 1024 samples = 64 ms; with
/// the 2-slot channel queue the speaker holds up to 128 ms of queued audio.
static constexpr size_t CHUNK_SAMPLES = 1024;
/// Chunk size in bytes (int16_t = 2 bytes per sample).
static constexpr size_t CHUNK_BYTES = CHUNK_SAMPLES * sizeof(int16_t);

/// Rotating chunk buffers handed to playRaw. playRaw does NOT copy: the
/// speaker keeps a pointer while a buffer is playing or queued, so a buffer
/// may only be reused after it leaves the 2-slot channel queue. Three buffers
/// in rotation is the pattern recommended by the M5Unified playRaw docs for
/// runtime-generated data.
static constexpr size_t NUM_CHUNK_BUFS = 3;
static int16_t s_chunks[NUM_CHUNK_BUFS][CHUNK_SAMPLES];
static uint8_t s_chunk_idx = 0;
/// True once the first chunk of the current utterance has been queued.
static std::atomic<bool> s_fed_since_start{false};

/// Read up to `bytes` (<= CHUNK_BYTES) from the ring and queue them on
/// speaker channel 0. Returns true if a chunk was queued. Feed-task only.
static bool feed_one_chunk(size_t bytes) {
    if (bytes > CHUNK_BYTES) bytes = CHUNK_BYTES;
    size_t n = ring_read(reinterpret_cast<uint8_t*>(s_chunks[s_chunk_idx]), bytes);
    size_t samples = n / sizeof(int16_t);
    if (samples == 0) return false;
    M5.Speaker.playRaw(s_chunks[s_chunk_idx], samples, SAMPLE_RATE, false, 1, 0, false);
    s_chunk_idx = (s_chunk_idx + 1) % NUM_CHUNK_BUFS;
    s_diag_feed_count++;
    s_fed_since_start.store(true, std::memory_order_relaxed);
    return true;
}

// ---------------------------------------------------------------------------
// Dedicated feed task
//
// Feeding used to run in loop(), so any loop() stall (measured: ws_poll up to
// 1543 ms while the ring held ~4 s of audio) starved the speaker queue and
// caused audible gaps. This task keeps the queue full independently of
// loop(), so playback rides out stalls up to the ring occupancy (~prebuffer).
// It also performs the drain on stop so the tail stays gapless.
// ---------------------------------------------------------------------------

static TaskHandle_t s_feed_task = nullptr;

static void feed_task_fn(void*) {
    bool was_silent = true;
    for (;;) {
        if (s_playing.load(std::memory_order_acquire) &&
            s_abort_requested.load(std::memory_order_acquire)) {
            // Abort: discard what is left in the ring (the
            // consumer may advance read_pos up to write_pos; the producer is
            // blocked in audio_playback_abort()), cut the speaker off, and shut
            // down without draining.
            s_read_pos.store(s_write_pos.load(std::memory_order_acquire),
                             std::memory_order_release);
            M5.Speaker.stop();
            M5.Speaker.end();
            was_silent = true;
            s_abort_requested.store(false, std::memory_order_release);
            s_stop_requested.store(false, std::memory_order_release);
            s_playing.store(false, std::memory_order_release);
        }
        if (s_playing.load(std::memory_order_acquire)) {
            // Count audible gap *events*: speaker fell fully silent after
            // streaming started (drain phase excluded).
            bool silent = (M5.Speaker.isPlaying(0) == 0);
            if (silent && !was_silent && s_fed_since_start.load(std::memory_order_relaxed) &&
                !s_stop_requested.load(std::memory_order_relaxed)) {
                s_diag_underrun_count++;
            }
            was_silent = silent;
            // Keep the channel queue full (one playing + one queued).
            while (M5.Speaker.isPlaying(0) < 2 && ring_available() >= CHUNK_BYTES) {
                feed_one_chunk(CHUNK_BYTES);
                was_silent = false;
            }
            // Drain and shut down when stop was requested and the ring is
            // down to less than a full chunk.
            if (s_stop_requested.load(std::memory_order_acquire) &&
                ring_available() < CHUNK_BYTES) {
                size_t remaining = ring_available();
                remaining -= remaining % sizeof(int16_t);
                if (remaining > 0) {
                    while (M5.Speaker.isPlaying(0) >= 2) {
                        vTaskDelay(1);
                    }
                    feed_one_chunk(remaining);
                }
                while (M5.Speaker.isPlaying()) {
                    vTaskDelay(1);
                }
                M5.Speaker.end();
                was_silent = true;
                s_stop_requested.store(false, std::memory_order_release);
                s_playing.store(false, std::memory_order_release);
            }
        }
        vTaskDelay(1);
    }
}

// ---------------------------------------------------------------------------
// Public API
// ---------------------------------------------------------------------------

bool audio_playback_start() {
    if (s_playing.load(std::memory_order_acquire)) {
        // Defensive: new speak_start while a previous drain is in flight.
        audio_playback_stop();
    }
    ring_init();
    if (!s_feed_task) {
        // Core 1 (same as loop task) at higher priority than loopTask (1), so
        // feeding preempts loop() instead of waiting for it.
        xTaskCreatePinnedToCore(feed_task_fn, "audio_feed", 4096, nullptr, 3, &s_feed_task, 1);
    }
    M5.Mic.end();
    bool ok = M5.Speaker.begin();
    ring_clear();
    ring_diag_reset();
    s_chunk_idx = 0;
    s_fed_since_start.store(false, std::memory_order_relaxed);
    s_stop_requested.store(false, std::memory_order_release);
    s_abort_requested.store(false, std::memory_order_release);
    s_playing.store(ok, std::memory_order_release);
    return ok;
}

void audio_playback_stop() {
    if (!s_playing.load(std::memory_order_acquire)) {
        return;
    }
    // Hand the drain to the feed task and wait: it plays the ring out
    // gaplessly, waits for the speaker to finish, calls M5.Speaker.end(),
    // then clears s_playing. Blocking here preserves the speak_end contract
    // (mic must not restart while the speaker owns the shared I2S port).
    s_stop_requested.store(true, std::memory_order_release);
    while (s_playing.load(std::memory_order_acquire)) {
        delay(1);
    }
}

void audio_playback_abort() {
    if (!s_playing.load(std::memory_order_acquire)) {
        return;
    }
    // The feed task discards the ring, stops the speaker, calls
    // M5.Speaker.end() and clears s_playing. The wait is at most one feed-task
    // iteration plus the chunk it may be queueing (a few ms), not the ring.
    s_abort_requested.store(true, std::memory_order_release);
    while (s_playing.load(std::memory_order_acquire)) {
        delay(1);
    }
}

bool audio_playback_is_active() {
    return s_playing.load(std::memory_order_acquire);
}

size_t audio_playback_feed(const uint8_t* data, size_t len) {
    return ring_write(data, len);
}

void audio_playback_diag_report(const char* tag) {
    Serial.printf(
        "[Playback] diag(%s): overflow_bytes=%lu underrun=%lu feeds=%lu "
        "max_occupancy=%u misaligned=%d\n",
        tag ? tag : "", static_cast<unsigned long>(s_diag_overflow_bytes),
        static_cast<unsigned long>(s_diag_underrun_count),
        static_cast<unsigned long>(s_diag_feed_count), static_cast<unsigned>(s_diag_max_occupancy),
        s_diag_misaligned ? 1 : 0);
}
