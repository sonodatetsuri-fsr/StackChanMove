#pragma once

#include <cstddef>
#include <cstdint>

/// Stop speaker, start mic. Returns true on success.
bool audio_capture_start();

/// Stop mic.
void audio_capture_stop();

/// Returns true while capture is active.
bool audio_capture_is_active();

/// Start recording `samples` int16 samples into `buf`.
/// Returns true if M5.Mic.record() accepted the request.
/// Caller should poll M5.Mic.isRecording() to know when the buffer is filled.
bool audio_capture_read(int16_t* buf, size_t samples);
