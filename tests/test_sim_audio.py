"""シミュレータの音声模擬 (WAV 変換・listen に従うマイク送信・--speech-out)。"""

import json
import struct
import wave

import numpy as np
import pytest
import sim_device
from sim_device import (
    MIC_CHUNK_BYTES,
    MIC_RATE,
    DiffDriveSim,
    MicStreamer,
    SimDevice,
    SpeechRecorder,
    build_pcm_frame,
    load_wav_pcm16_mono,
)


def write_wav(path, samples: np.ndarray, rate: int, channels: int = 1, sampwidth: int = 2) -> None:
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(channels)
        wf.setsampwidth(sampwidth)
        wf.setframerate(rate)
        wf.writeframes(samples.tobytes())


def test_wav_24k_mono_is_converted_to_16k(tmp_path):
    path = tmp_path / "a.wav"
    write_wav(path, np.full(24000, 1000, dtype=np.int16), 24000)
    pcm = load_wav_pcm16_mono(str(path))
    samples = np.frombuffer(pcm, dtype=np.int16)
    assert len(samples) == 16000
    assert np.all(samples == 1000)


def test_wav_48k_stereo_is_downmixed(tmp_path):
    path = tmp_path / "b.wav"
    stereo = np.empty(48000 * 2, dtype=np.int16)
    stereo[0::2] = 2000
    stereo[1::2] = 0
    write_wav(path, stereo, 48000, channels=2)
    samples = np.frombuffer(load_wav_pcm16_mono(str(path)), dtype=np.int16)
    assert len(samples) == 16000
    assert np.all(samples == 1000)


def test_wav_8bit_is_converted(tmp_path):
    path = tmp_path / "c.wav"
    write_wav(path, np.full(16000, 128 + 64, dtype=np.uint8), 16000, sampwidth=1)
    samples = np.frombuffer(load_wav_pcm16_mono(str(path)), dtype=np.int16)
    assert len(samples) == 16000
    assert np.all(samples == 64 * 256)


def _chunks(mic: MicStreamer, n: int) -> list[bytes | None]:
    return [mic.next_chunk() for _ in range(n)]


def test_mic_waits_for_listen_on_then_streams_and_stops():
    pcm = b"\x01\x00" * 512 * 3  # 3 chunks
    mic = MicStreamer(pcm)
    assert mic.next_chunk() is None  # listen 未受信
    mic.listening = True
    tail_chunks = int(1.5 * MIC_RATE) * 2 // MIC_CHUNK_BYTES + 1
    chunks = _chunks(mic, 3 + tail_chunks)
    assert all(c is not None and len(c) == MIC_CHUNK_BYTES for c in chunks)
    assert chunks[0] == b"\x01\x00" * 512
    assert chunks[3] == b"\x00" * MIC_CHUNK_BYTES  # 末尾の無音
    assert mic.finished
    assert mic.next_chunk() is None  # --mic-loop なしは 1 回だけ
    assert mic.passes == 1


def test_mic_pauses_on_listen_off_and_resumes_at_same_position():
    pcm = b"".join(bytes([i, 0]) * 512 for i in range(1, 5))  # 4 distinct chunks
    mic = MicStreamer(pcm)
    mic.listening = True
    assert mic.next_chunk() == b"\x01\x00" * 512
    mic.listening = False
    assert mic.next_chunk() is None
    assert mic.next_chunk() is None
    mic.listening = True
    assert mic.next_chunk() == b"\x02\x00" * 512


def test_mic_loop_repeats_with_3s_gap():
    pcm = b"\x05\x00" * 512
    mic = MicStreamer(pcm, loop=True)
    mic.listening = True
    gap_chunks = -(-int(3.0 * MIC_RATE) * 2 // MIC_CHUNK_BYTES)
    first = mic.next_chunk()
    gap = _chunks(mic, gap_chunks)
    again = mic.next_chunk()
    assert first == again == b"\x05\x00" * 512
    assert all(c == b"\x00" * MIC_CHUNK_BYTES for c in gap)
    assert not mic.finished
    assert mic.passes == 1


def test_build_pcm_frame_header():
    frame = build_pcm_frame(b"\x01\x02", 0x1_0000_0005)
    assert frame[0] == 0x01
    assert struct.unpack("<I", frame[1:5])[0] == 5
    assert frame[5:] == b"\x01\x02"


def test_listen_messages_toggle_the_mic():
    mic = MicStreamer(b"\x00\x00" * 512)
    device = SimDevice(DiffDriveSim(), "ws://x/ws/device", 10.0, mic=mic)
    device.handle_text(json.dumps({"type": "listen", "on": True}))
    assert mic.listening is True
    device.handle_text(json.dumps({"type": "listen", "on": False}))
    assert mic.listening is False


def test_speech_out_writes_wav_between_speak_start_and_end(tmp_path, caplog):
    caplog.set_level("INFO", logger="sim_device")
    out = tmp_path / "speech"
    device = SimDevice(DiffDriveSim(), "ws://x/ws/device", 10.0, speech=SpeechRecorder(str(out)))
    device.handle_binary(build_pcm_frame(b"\x09\x00" * 100, 1))  # speak_start 前は捨てる
    device.handle_text(json.dumps({"type": "speak_start"}))
    device.handle_binary(build_pcm_frame(b"\x01\x00" * 8000, 2))
    device.handle_binary(build_pcm_frame(b"\x02\x00" * 8000, 3))
    device.handle_binary(sim_device.build_camera_frame(b"jpeg", 4))  # JPEG は混ぜない
    device.handle_text(json.dumps({"type": "speak_end"}))
    device.handle_text(json.dumps({"type": "speak_start"}))
    device.handle_text(json.dumps({"type": "speak_end"}))

    first = out / "speech_001.wav"
    with wave.open(str(first), "rb") as wf:
        assert (wf.getnchannels(), wf.getsampwidth(), wf.getframerate()) == (1, 2, 16000)
        frames = wf.readframes(wf.getnframes())
    assert frames == b"\x01\x00" * 8000 + b"\x02\x00" * 8000
    assert (out / "speech_002.wav").exists()
    assert f"[Sim] speech saved: {first} (1.0 s)" in caplog.text


def test_heard_is_logged(caplog):
    caplog.set_level("INFO", logger="sim_device")
    device = SimDevice(DiffDriveSim(), "ws://x/ws/device", 10.0)
    device.handle_text(json.dumps({"type": "heard", "text": "こんにちは", "intent": ""}))
    assert "[Sim] heard: こんにちは" in caplog.text


def test_parser_audio_options():
    args = sim_device.build_parser().parse_args(["--mic-wav", "a.wav", "--mic-loop", "--speech-out", "out"])
    assert (args.mic_wav, args.mic_loop, args.speech_out) == ("a.wav", True, "out")
    args = sim_device.build_parser().parse_args([])
    assert (args.mic_wav, args.mic_loop, args.speech_out) == (None, False, None)


class FakeWs:
    def __init__(self) -> None:
        self.sent: list[bytes] = []

    async def send(self, data) -> None:
        self.sent.append(data)


@pytest.mark.asyncio
async def test_mic_loop_sends_frames_in_real_time_only_while_listening():
    import asyncio

    mic = MicStreamer(b"\x01\x00" * 512 * 4)
    device = SimDevice(DiffDriveSim(), "ws://x/ws/device", 10.0, mic=mic)
    ws = FakeWs()
    task = asyncio.create_task(device.mic_loop(ws))
    await asyncio.sleep(0.1)
    assert ws.sent == []  # listen 未受信
    mic.listening = True
    await asyncio.sleep(0.1)
    sent = len(ws.sent)
    assert 2 <= sent <= 5  # 32 ms 間隔 (実時間)
    assert all(f[0] == 0x01 and len(f) == 5 + MIC_CHUNK_BYTES for f in ws.sent)
    mic.listening = False
    await asyncio.sleep(0.1)
    assert len(ws.sent) <= sent + 1
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
