#include "audio_capture.h"

#include <M5Unified.h>

#include "config.h"

static bool s_capturing = false;

bool audio_capture_start() {
    if (s_capturing) {
        M5.Mic.end();
        s_capturing = false;
    }
    M5.Speaker.end();
    bool ok = M5.Mic.begin();
    s_capturing = ok;
    return ok;
}

void audio_capture_stop() {
    M5.Mic.end();
    s_capturing = false;
}

bool audio_capture_is_active() {
    return s_capturing;
}

bool audio_capture_read(int16_t* buf, size_t samples) {
    if (!s_capturing) {
        return false;
    }
    return M5.Mic.record(buf, samples, SAMPLE_RATE, false);
}
