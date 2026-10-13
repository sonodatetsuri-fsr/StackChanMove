#pragma once

#include <cstddef>
#include <cstdint>

/// Stop mic, start speaker, clear ring buffer, and (lazily) spawn the
/// dedicated feed task that streams the ring to the speaker independently of
/// loop(). Returns true on success.
bool audio_playback_start();

/// Request the feed task to drain the ring / wait for the speaker to finish,
/// then stop the speaker. Blocks until playback has fully ended (the mic must
/// not restart while the speaker owns the shared I2S port).
void audio_playback_stop();

/// Discard whatever is left in the ring and stop the speaker immediately
/// (control WS lost mid-utterance). The feed task drops the
/// ring, stops the speaker and calls M5.Speaker.end(); this blocks only until
/// it has done so (a few ms), unlike audio_playback_stop() which plays the
/// ring out. Returns immediately when not playing.
void audio_playback_abort();

/// Returns true while playback is active.
bool audio_playback_is_active();

/// Append raw PCM16 data (int16_t samples, little-endian) to the ring buffer.
/// Returns the number of bytes actually written (may be less than `len` if the
/// buffer is full).
size_t audio_playback_feed(const uint8_t* data, size_t len);

/// Print (over Serial) the playback diagnostics accumulated since the last
/// audio_playback_start(): overflow (dropped) bytes, underrun count (audible
/// gap events — the speaker fell fully silent after streaming started;
/// expected 0), chunks fed, peak ring occupancy, and whether the write
/// pointer ever landed on an odd byte (int16 misalignment => noise).
/// `tag` labels the line. Diagnostics instrumentation.
void audio_playback_diag_report(const char* tag);
