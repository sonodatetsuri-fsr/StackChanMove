"""(stackchan_move から移植して変更) SpeechQueue は TTS プラグインの TtsAudio を使う。"""

import asyncio
import json
import logging
from unittest.mock import AsyncMock, MagicMock

import numpy as np
import pytest

from openmovehub.config import Settings
from openmovehub.models.frames import HEADER_SIZE, KIND_PCM, decode_frame
from openmovehub.plugins.base import TtsAudio
from openmovehub.plugins.none import NoneTts
from openmovehub.services import speech_queue as sq_module
from openmovehub.services.connection_manager import ConnectionManager
from openmovehub.services.listen_state import ListenState
from openmovehub.services.resample import resample_pcm16
from openmovehub.services.speech_queue import SpeechQueue


def make_settings(**overrides):
    return Settings(**overrides)


def make_tts(audio: bytes = b"\x00" * 10, rate: int = 16000):
    tts = MagicMock()
    tts.name = "mock"
    tts.synthesize = AsyncMock(return_value=TtsAudio(audio, rate))
    return tts


def make_mock_ws():
    ws = AsyncMock()
    ws.accept = AsyncMock()
    ws.send_text = AsyncMock()
    ws.send_bytes = AsyncMock()
    return ws


def sent_payloads(ws):
    """Decode the binary frames sent to a device, returning their PCM payloads."""
    payloads = []
    for call in ws.send_bytes.await_args_list:
        kind, _ts_ms, payload = decode_frame(call.args[0])
        assert kind == KIND_PCM
        payloads.append(payload)
    return payloads


@pytest.mark.asyncio
async def test_enqueue_triggers_tts_and_audio_send():
    mock_tts = MagicMock()
    mock_tts.name = "mock"
    audio_data = b"\x00\x01" * 100
    mock_tts.synthesize = AsyncMock(return_value=TtsAudio(audio_data, 16000))

    manager = ConnectionManager()
    ws = make_mock_ws()
    await manager.connect(ws, "device")

    sq = SpeechQueue(mock_tts, manager, make_settings(), ListenState(on=True))
    sq.start()

    await sq.enqueue("テスト音声")
    await sq._queue.join()

    mock_tts.synthesize.assert_awaited_once_with("テスト音声")
    assert sent_payloads(ws) == [audio_data]

    await sq.stop()


@pytest.mark.asyncio
async def test_listen_off_before_speak_start():
    mock_tts = MagicMock()
    mock_tts.name = "mock"
    mock_tts.synthesize = AsyncMock(return_value=TtsAudio(b"\x00" * 10, 16000))

    manager = ConnectionManager()
    ws = make_mock_ws()
    await manager.connect(ws, "device")

    sq = SpeechQueue(mock_tts, manager, make_settings(), ListenState(on=True))
    sq.start()

    await sq.enqueue("hello")
    await sq._queue.join()

    sent_texts = [c.args[0] for c in ws.send_text.await_args_list]
    assert len(sent_texts) >= 2

    parsed = [json.loads(t) for t in sent_texts]
    types = [p["type"] for p in parsed]

    listen_off_idx = next(i for i, p in enumerate(parsed) if p["type"] == "listen" and not p["on"])
    speak_start_idx = types.index("speak_start")
    assert listen_off_idx < speak_start_idx

    await sq.stop()


@pytest.mark.asyncio
async def test_speak_end_before_listen_on():
    mock_tts = MagicMock()
    mock_tts.name = "mock"
    mock_tts.synthesize = AsyncMock(return_value=TtsAudio(b"\x00" * 10, 16000))

    manager = ConnectionManager()
    ws = make_mock_ws()
    await manager.connect(ws, "device")

    sq = SpeechQueue(mock_tts, manager, make_settings(), ListenState(on=True))
    sq.start()

    await sq.enqueue("hello")
    await sq._queue.join()

    sent_texts = [c.args[0] for c in ws.send_text.await_args_list]
    parsed = [json.loads(t) for t in sent_texts]
    types = [p["type"] for p in parsed]

    speak_end_idx = types.index("speak_end")
    listen_on_idx = next(i for i, p in enumerate(parsed) if p["type"] == "listen" and p["on"])
    assert speak_end_idx < listen_on_idx

    await sq.stop()


@pytest.mark.asyncio
async def test_message_order():
    mock_tts = MagicMock()
    mock_tts.name = "mock"
    audio_data = b"\x01\x02" * 50
    mock_tts.synthesize = AsyncMock(return_value=TtsAudio(audio_data, 16000))

    manager = ConnectionManager()
    ws = make_mock_ws()
    await manager.connect(ws, "device")

    sq = SpeechQueue(mock_tts, manager, make_settings(), ListenState(on=True))
    sq.start()

    await sq.enqueue("順番テスト")
    await sq._queue.join()

    text_calls = [c.args[0] for c in ws.send_text.await_args_list]
    parsed = [json.loads(t) for t in text_calls]
    types = [p["type"] for p in parsed]

    assert types[0] == "listen"
    assert not parsed[0]["on"]
    assert types[1] == "speak_start"
    assert types[2] == "speak_end"
    assert types[3] == "listen"
    assert parsed[3]["on"]

    assert sent_payloads(ws) == [audio_data]

    await sq.stop()


@pytest.mark.asyncio
async def test_worker_survives_tts_error():
    mock_tts = MagicMock()
    mock_tts.name = "mock"
    mock_tts.synthesize = AsyncMock(side_effect=Exception("TTS failure"))

    manager = ConnectionManager()
    ws = make_mock_ws()
    await manager.connect(ws, "device")

    sq = SpeechQueue(mock_tts, manager, make_settings(), ListenState(on=True))
    sq.start()

    await sq.enqueue("エラーテスト")
    await sq._queue.join()

    second_audio = b"\xff" * 20
    mock_tts.synthesize = AsyncMock(return_value=TtsAudio(second_audio, 16000))

    await sq.enqueue("回復テスト")
    await sq._queue.join()

    assert sent_payloads(ws) == [second_audio]

    await sq.stop()


@pytest.mark.asyncio
async def test_enqueue_with_emotion():
    mock_tts = MagicMock()
    mock_tts.name = "mock"
    mock_tts.synthesize = AsyncMock(return_value=TtsAudio(b"\x00" * 10, 16000))

    manager = ConnectionManager()
    sq = SpeechQueue(mock_tts, manager, make_settings(), ListenState(on=True))
    sq.start()

    await sq.enqueue("感情テスト", emotion="happy")
    await sq._queue.join()

    mock_tts.synthesize.assert_awaited_once_with("感情テスト")

    await sq.stop()


@pytest.mark.asyncio
async def test_long_audio_is_paced_into_slices():
    # Small prebuffer/slice so audio larger than the prebuffer is split.
    settings = make_settings(speak_prebuffer_s=0.002, speak_slice_s=0.001)
    audio_data = bytes(range(256)) * 4  # 1024 bytes, well above the 64-byte prebuffer

    mock_tts = MagicMock()
    mock_tts.name = "mock"
    mock_tts.synthesize = AsyncMock(return_value=TtsAudio(audio_data, 16000))

    manager = ConnectionManager()
    ws = make_mock_ws()
    await manager.connect(ws, "device")

    sq = SpeechQueue(mock_tts, manager, settings, ListenState(on=True))
    sq.start()

    await sq.enqueue("長文発話")
    await sq._queue.join()

    binary_chunks = sent_payloads(ws)
    # Paced into more than one frame, and the full audio is reconstructed in order.
    assert len(binary_chunks) > 1
    assert b"".join(binary_chunks) == audio_data
    # the 64-byte prebuffer (ceil(int(0.002*32000)/32) * 32) is split into
    # slice-sized frames of 32 bytes (header excluded).
    assert len(binary_chunks[0]) == 32
    assert len(binary_chunks[1]) == 32

    await sq.stop()


@pytest.mark.asyncio
async def test_short_audio_sent_in_single_frame():
    # Audio smaller than the prebuffer is sent as one frame (no pacing).
    audio_data = b"\x00\x01" * 100  # 200 bytes < default 64000-byte prebuffer

    mock_tts = MagicMock()
    mock_tts.name = "mock"
    mock_tts.synthesize = AsyncMock(return_value=TtsAudio(audio_data, 16000))

    manager = ConnectionManager()
    ws = make_mock_ws()
    await manager.connect(ws, "device")

    sq = SpeechQueue(mock_tts, manager, make_settings(), ListenState(on=True))
    sq.start()

    await sq.enqueue("短文")
    await sq._queue.join()

    assert sent_payloads(ws) == [audio_data]

    await sq.stop()


# --- TTS 音声の送出 ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_24k_tts_audio_is_resampled_to_16k_before_sending():
    audio_24k = (np.arange(2400) % 1000).astype(np.int16).tobytes()  # 0.1 s @ 24 kHz
    tts = make_tts(audio_24k, rate=24000)

    manager = ConnectionManager()
    ws = make_mock_ws()
    await manager.connect(ws, "device")

    sq = SpeechQueue(tts, manager, make_settings(), ListenState(on=True))
    sq.start()
    await sq.enqueue("変換")
    await sq._queue.join()

    sent = b"".join(sent_payloads(ws))
    assert len(sent) == 1600 * 2  # 0.1 s @ 16 kHz
    assert sent == resample_pcm16(audio_24k, 24000, 16000)
    await sq.stop()


@pytest.mark.asyncio
async def test_synthesize_exception_skips_speak_and_restores_listen():
    tts = make_tts()
    tts.synthesize = AsyncMock(side_effect=RuntimeError("engine down"))

    manager = ConnectionManager()
    ws = make_mock_ws()
    await manager.connect(ws, "device")

    sq = SpeechQueue(tts, manager, make_settings(), ListenState(on=True))
    sq.start()
    await sq.enqueue("失敗")
    await sq._queue.join()

    parsed = [json.loads(c.args[0]) for c in ws.send_text.await_args_list]
    assert parsed == [
        {"type": "listen", "on": False},
        {"type": "listen", "on": True},
    ]
    assert sent_payloads(ws) == []
    await sq.stop()


@pytest.mark.asyncio
async def test_speak_start_is_not_sent_until_synthesis_finishes():
    release = asyncio.Event()
    started = asyncio.Event()
    tts = make_tts()

    async def slow_synth(text):
        started.set()
        await release.wait()
        return TtsAudio(b"\x00" * 10, 16000)

    tts.synthesize = AsyncMock(side_effect=slow_synth)
    manager = ConnectionManager()
    ws = make_mock_ws()
    await manager.connect(ws, "device")

    sq = SpeechQueue(tts, manager, make_settings(), ListenState(on=True))
    sq.start()
    await sq.enqueue("遅い合成")
    await asyncio.wait_for(started.wait(), 1)
    for _ in range(5):
        await asyncio.sleep(0)

    # 合成中に送られたのは listen off だけ。
    assert [json.loads(c.args[0]) for c in ws.send_text.await_args_list] == [{"type": "listen", "on": False}]
    assert ws.send_bytes.await_args_list == []

    release.set()
    await sq._queue.join()
    types = [json.loads(c.args[0])["type"] for c in ws.send_text.await_args_list]
    assert types == ["listen", "speak_start", "speak_end", "listen"]
    await sq.stop()


@pytest.mark.asyncio
async def test_send_exception_after_speak_start_still_sends_speak_end_and_listen():
    tts = make_tts()
    manager = ConnectionManager()
    ws = make_mock_ws()
    await manager.connect(ws, "device")

    sq = SpeechQueue(tts, manager, make_settings(), ListenState(on=True))
    real_send = manager.send_to_devices

    # 音声フレームの送信 (_send_paced 中の send_to_devices) だけ失敗させる。
    async def failing_send(data):
        if isinstance(data, bytes):
            raise RuntimeError("send failed")
        await real_send(data)

    manager.send_to_devices = failing_send
    sq.start()
    await sq.enqueue("送信失敗")
    await sq._queue.join()

    parsed = [json.loads(c.args[0]) for c in ws.send_text.await_args_list]
    assert parsed == [
        {"type": "listen", "on": False},
        {"type": "speak_start"},
        {"type": "speak_end"},
        {"type": "listen", "on": True},
    ]
    await sq.stop()


@pytest.mark.asyncio
async def test_listen_is_restored_to_current_flag_off():
    listen_state = ListenState(on=True)
    tts = make_tts()

    async def synth(text):
        listen_state.on = False  # 発話中に OFF にされた
        return TtsAudio(b"\x00" * 10, 16000)

    tts.synthesize = AsyncMock(side_effect=synth)
    manager = ConnectionManager()
    ws = make_mock_ws()
    await manager.connect(ws, "device")
    sq = SpeechQueue(tts, manager, make_settings(), listen_state)
    sq.start()
    await sq.enqueue("x")
    await sq._queue.join()
    parsed = [json.loads(c.args[0]) for c in ws.send_text.await_args_list]
    assert parsed[-1] == {"type": "listen", "on": False}
    await sq.stop()


@pytest.mark.asyncio
async def test_none_tts_drops_speak_without_speak_start(caplog):
    manager = ConnectionManager()
    ws = make_mock_ws()
    await manager.connect(ws, "device")
    sq = SpeechQueue(NoneTts(), manager, make_settings(), ListenState(on=True))
    sq.start()
    await sq.enqueue("捨てられる")
    await sq._queue.join()
    assert ws.send_text.await_args_list == []
    assert ws.send_bytes.await_args_list == []
    assert "TTS plugin is none" in caplog.text
    await sq.stop()


# --- 合成と送出の順序 ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_prebuffer_is_split_into_slice_sized_frames(monkeypatch, caplog):
    """先送り 2.0 s は 16,000 B (+ヘッダ) のフレーム 4 つで間を空けずに送り、後続は従来どおり実時間ペース。"""
    settings = make_settings()  # speak_prebuffer_s=2.0, speak_slice_s=0.5 → 16,000 B / slice
    # 3.0 s の音声 = 先送り 64,000 B + 実時間のスライス 2 つ (16,000 B)。
    audio_data = (np.arange(48000) % 30000).astype(np.int16).tobytes()

    events: list[tuple[str, object]] = []
    real_sleep = asyncio.sleep

    async def fake_sleep(delay, *args, **kwargs):
        events.append(("sleep", delay))
        await real_sleep(0)

    monkeypatch.setattr(sq_module.asyncio, "sleep", fake_sleep)

    manager = ConnectionManager()
    ws = make_mock_ws()

    async def record_bytes(data):
        events.append(("frame", len(data)))

    ws.send_bytes = AsyncMock(side_effect=record_bytes)
    await manager.connect(ws, "device")

    sq = SpeechQueue(make_tts(audio_data), manager, settings, ListenState(on=True))
    with caplog.at_level(logging.INFO):
        await sq._send_paced(audio_data)

    frame_sizes = [n for kind, n in events if kind == "frame"]
    assert frame_sizes == [16000 + HEADER_SIZE] * 6
    # 先送りの 4 フレームの間には sleep が無く、以降は各スライスの前に slice_s の sleep が入る。
    assert events == [
        ("frame", 16000 + HEADER_SIZE),
        ("frame", 16000 + HEADER_SIZE),
        ("frame", 16000 + HEADER_SIZE),
        ("frame", 16000 + HEADER_SIZE),
        ("sleep", 0.5),
        ("frame", 16000 + HEADER_SIZE),
        ("sleep", 0.5),
        ("frame", 16000 + HEADER_SIZE),
    ]
    assert b"".join(sent_payloads(ws)) == audio_data
    assert "prebuffer=64000 bytes, 2 paced slices, prebuffer_frames=4" in caplog.text
