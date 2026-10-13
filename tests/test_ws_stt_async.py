"""(stackchan_move から移植) WebSocket の STT ワーカー (latest-wins・heard・heard フック)。

STT は ``SttRunner`` 経由でダミープラグインを呼ぶ (実モデルはロードしない)。
"""

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from openmovehub.config import Settings
from openmovehub.models.frames import KIND_PCM, encode_frame
from openmovehub.plugins.none import NoneStt
from openmovehub.routers.ws import websocket_endpoint
from openmovehub.services.anim_state import AnimState
from openmovehub.services.connection_manager import ConnectionManager
from openmovehub.services.drive_controller import DriveController
from openmovehub.services.listen_state import ListenState
from openmovehub.services.speech_queue import SpeechQueue
from openmovehub.services.stt_runner import SttRunner


def _drive(manager: ConnectionManager) -> DriveController:
    """A fresh DriveController for the endpoint under test; its repeater is not started."""
    return DriveController(manager, Settings())


def _ws(receive) -> MagicMock:
    ws = MagicMock()
    ws.accept = AsyncMock()
    ws.send_text = AsyncMock()
    ws.send_bytes = AsyncMock()
    ws.close = AsyncMock()
    ws.receive = AsyncMock(side_effect=receive)
    return ws


def _ready_runner(plugin) -> SttRunner:
    runner = SttRunner(plugin, label="mock")
    runner._state = "ready"
    return runner


@pytest.fixture
def manager():
    return ConnectionManager()


@pytest.fixture
def mock_stt():
    stt = MagicMock()
    stt.name = "mock"
    stt.transcribe = MagicMock(return_value="テスト")
    stt.status = MagicMock(return_value={})
    return stt


@pytest.fixture
def mock_speech_queue():
    sq = MagicMock(spec=SpeechQueue)
    sq.enqueue = AsyncMock()
    return sq


@pytest.fixture
def mock_vad():
    vad = MagicMock()
    accumulator = MagicMock()
    accumulator.feed = MagicMock(side_effect=lambda chunk: chunk)
    vad.create_accumulator = MagicMock(return_value=accumulator)
    return vad


@pytest.fixture
def listen_state():
    return ListenState(on=True)


@pytest.fixture
def anim_state():
    return AnimState(mouth=True)


async def _run(ws, role, manager, listen_state, anim_state, runner, vad, speech_queue):
    await websocket_endpoint(
        ws,
        role,
        manager,
        listen_state,
        anim_state,
        _drive(manager),
        stt_runner=runner,
        vad=vad,
        speech_queue=speech_queue,
    )


def _frames_then_disconnect(*frames: bytes, wait: float = 0.1):
    queue = [{"type": "websocket.bytes", "bytes": encode_frame(KIND_PCM, f)} for f in frames]

    async def receive():
        if queue:
            return queue.pop(0)
        await asyncio.sleep(wait)
        return {"type": "websocket.disconnect"}

    return receive


async def test_stt_processes_audio_frame(manager, mock_stt, mock_speech_queue, mock_vad, listen_state, anim_state):
    """Single audio frame is transcribed (16 kHz) and heard is broadcast to apps and devices."""
    audio_data = b"\x00\x01" * 50
    app_ws = _ws([{"type": "websocket.disconnect"}])
    await manager.connect(app_ws, "app")
    ws = _ws(_frames_then_disconnect(audio_data))
    await _run(ws, "device", manager, listen_state, anim_state, _ready_runner(mock_stt), mock_vad, mock_speech_queue)

    mock_stt.transcribe.assert_called_once_with(audio_data, 16000)
    heard = {"type": "heard", "text": "テスト", "intent": ""}
    assert heard in [json.loads(c.args[0]) for c in ws.send_text.call_args_list]
    assert heard in [json.loads(c.args[0]) for c in app_ws.send_text.call_args_list]


async def test_media_role_audio_is_transcribed(
    manager, mock_stt, mock_speech_queue, mock_vad, listen_state, anim_state
):
    ws = _ws(_frames_then_disconnect(b"\x00\x01" * 50))
    await _run(ws, "media", manager, listen_state, anim_state, _ready_runner(mock_stt), mock_vad, mock_speech_queue)
    mock_stt.transcribe.assert_called_once()


async def test_latest_wins_drops_stale_frames(manager, mock_stt, mock_speech_queue, mock_vad, listen_state, anim_state):
    """When STT is slow, only the latest audio frame should be transcribed."""
    frame_a = b"\xaa" * 100
    frame_b = b"\xbb" * 100
    frame_c = b"\xcc" * 100  # This is the latest — should win
    ws = _ws(_frames_then_disconnect(frame_a, frame_b, frame_c))
    await _run(ws, "device", manager, listen_state, anim_state, _ready_runner(mock_stt), mock_vad, mock_speech_queue)

    calls = mock_stt.transcribe.call_args_list
    assert calls[-1].args[0] == frame_c
    assert len(calls) <= 2  # At most 2 calls (not 3)


async def test_text_messages_not_blocked_by_stt(
    manager, mock_stt, mock_speech_queue, mock_vad, listen_state, anim_state
):
    """Text messages (SpeakMessage) are handled without waiting for STT."""
    speak_json = json.dumps({"type": "speak", "text": "hello", "emotion": "happy"})

    def slow_transcribe(audio: bytes, sample_rate: int) -> str:
        import time

        time.sleep(0.2)
        return "テスト"

    mock_stt.transcribe = MagicMock(side_effect=slow_transcribe)
    queue = [
        {"type": "websocket.bytes", "bytes": encode_frame(KIND_PCM, b"\x00\x01" * 50)},
        {"type": "websocket.text", "text": speak_json},
    ]

    async def receive():
        if queue:
            return queue.pop(0)
        await asyncio.sleep(0.3)
        return {"type": "websocket.disconnect"}

    ws = _ws(receive)
    await _run(ws, "device", manager, listen_state, anim_state, _ready_runner(mock_stt), mock_vad, mock_speech_queue)
    mock_speech_queue.enqueue.assert_called_once_with("hello", "happy")


async def test_stt_worker_cancelled_on_disconnect(
    manager, mock_stt, mock_speech_queue, mock_vad, listen_state, anim_state
):
    """STT worker task is properly cancelled when the WebSocket disconnects."""

    def blocking_transcribe(audio: bytes, sample_rate: int) -> str:
        import time

        time.sleep(1)
        return "テスト"

    mock_stt.transcribe = MagicMock(side_effect=blocking_transcribe)
    ws = _ws(_frames_then_disconnect(b"\x00" * 100, wait=0.0))
    await asyncio.wait_for(
        _run(ws, "device", manager, listen_state, anim_state, _ready_runner(mock_stt), mock_vad, mock_speech_queue),
        timeout=3.0,
    )


async def test_audio_before_ready_is_dropped(manager, mock_stt, mock_speech_queue, mock_vad, listen_state, anim_state):
    runner = SttRunner(mock_stt, label="mock")  # loading のまま
    ws = _ws(_frames_then_disconnect(b"\x00\x01" * 50))
    await _run(ws, "device", manager, listen_state, anim_state, runner, mock_vad, mock_speech_queue)
    mock_stt.transcribe.assert_not_called()
    assert runner.dropped_not_ready == 1
    assert not any(json.loads(c.args[0])["type"] == "heard" for c in ws.send_text.call_args_list)


async def test_listen_off_skips_vad(manager, mock_stt, mock_speech_queue, mock_vad, anim_state):
    ws = _ws(_frames_then_disconnect(b"\x00\x01" * 50))
    await _run(
        ws, "device", manager, ListenState(on=False), anim_state, _ready_runner(mock_stt), mock_vad, mock_speech_queue
    )
    mock_vad.create_accumulator.return_value.feed.assert_not_called()
    mock_stt.transcribe.assert_not_called()


async def test_none_stt_skips_vad(manager, mock_speech_queue, mock_vad, listen_state, anim_state):
    runner = SttRunner(NoneStt(), label="none")
    ws = _ws(_frames_then_disconnect(b"\x00\x01" * 50))
    await _run(ws, "device", manager, listen_state, anim_state, runner, mock_vad, mock_speech_queue)
    mock_vad.create_accumulator.return_value.feed.assert_not_called()


# --- heard フック (stackchan_move の explore.on_heard と同じ位置) ------------------------


async def _heard_messages(manager, mock_stt, mock_speech_queue, mock_vad, listen_state, anim_state, hook):
    runner = _ready_runner(mock_stt)
    runner.heard_hook = hook
    ws = _ws(_frames_then_disconnect(b"\x00\x01" * 50))
    await _run(ws, "device", manager, listen_state, anim_state, runner, mock_vad, mock_speech_queue)
    sent = [json.loads(c.args[0]) for c in ws.send_text.call_args_list]
    return [m for m in sent if m.get("type") == "heard"]


async def test_heard_intent_comes_from_the_hook(
    manager, mock_stt, mock_speech_queue, mock_vad, listen_state, anim_state
):
    mock_stt.transcribe = MagicMock(return_value="これは何")
    hook = AsyncMock(return_value="vlm:ask")
    heard = await _heard_messages(manager, mock_stt, mock_speech_queue, mock_vad, listen_state, anim_state, hook)
    assert heard == [{"type": "heard", "text": "これは何", "intent": "vlm:ask"}]
    hook.assert_awaited_once_with("これは何")


async def test_heard_intent_is_empty_without_hook(
    manager, mock_stt, mock_speech_queue, mock_vad, listen_state, anim_state
):
    heard = await _heard_messages(manager, mock_stt, mock_speech_queue, mock_vad, listen_state, anim_state, None)
    assert heard == [{"type": "heard", "text": "テスト", "intent": ""}]


async def test_heard_is_sent_even_if_the_hook_fails(
    manager, mock_stt, mock_speech_queue, mock_vad, listen_state, anim_state
):
    hook = AsyncMock(side_effect=RuntimeError("boom"))
    heard = await _heard_messages(manager, mock_stt, mock_speech_queue, mock_vad, listen_state, anim_state, hook)
    assert heard == [{"type": "heard", "text": "テスト", "intent": ""}]


async def test_empty_text_is_not_broadcast(manager, mock_stt, mock_speech_queue, mock_vad, listen_state, anim_state):
    mock_stt.transcribe = MagicMock(return_value="  ")
    heard = await _heard_messages(manager, mock_stt, mock_speech_queue, mock_vad, listen_state, anim_state, None)
    assert heard == []
