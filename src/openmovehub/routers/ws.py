"""WebSocket エンドポイント (``/ws/{device|app|media}``)。

- ``device``: Hub→端末のテキスト全部、端末→Hub の ``odom`` / ``joy`` / ``event``。
- ``media``: 端末→Hub のバイナリのみ (マイク PCM ``0x01`` / カメラ JPEG ``0x02``)。
  ``device`` ロールのバイナリも互換のため受け付ける。
- ``app``: ブラウザ・CLI。app からのバイナリは破棄する。

音声は stackchan_move と同じ経路。device / media のマイク PCM (``0x01``) を接続ごとの
Silero VAD で発話に切り出し、latest-wins で STT ワーカーに渡す (``SttRunner`` 経由でプラグインを呼ぶ)。
結果は ``heard{text,intent}`` として全 app と全 device に送る。``speak`` は ``SpeechQueue`` に積む。
app の greeting に ``state stt:<loading|ready|error|off>`` を含める。

app の ``vlm_ask{question}`` を ``VlmQaService`` に積む (入力元 ``text``)。app の greeting に
``state vlm:*``・``vlm_voice_in:*``・``vlm_voice_out:*`` を含める。
"""

import asyncio
import json
import logging
from typing import Literal

from starlette.websockets import WebSocket, WebSocketDisconnect

from openmovehub.config import clamp_odom_hz
from openmovehub.models.frames import KIND_JPEG, KIND_PCM, decode_frame
from openmovehub.models.messages import (
    AnimMessage,
    ControlMessage,
    DriveSourceMessage,
    EventMessage,
    HeardMessage,
    JoyMessage,
    ListenMessage,
    OdomMessage,
    OdomRateMessage,
    SpeakMessage,
    StateMessage,
    VlmAskMessage,
    parse_message,
)
from openmovehub.routers.control import handle_control
from openmovehub.services.anim_state import AnimState
from openmovehub.services.camera_relay import CameraRelay
from openmovehub.services.connection_manager import ConnectionManager
from openmovehub.services.drive_controller import DriveController
from openmovehub.services.listen_state import ListenState
from openmovehub.services.speech_queue import SpeechQueue
from openmovehub.services.stt_runner import SttRunner
from openmovehub.services.vad import VADService
from openmovehub.services.vlm_qa import VlmQaService

logger = logging.getLogger(__name__)

Role = Literal["device", "app", "media"]

#: greeting の ``odom_rate`` の既定値 (``HUB_ODOM_HZ``)。
DEFAULT_ODOM_HZ = 10


async def websocket_endpoint(
    ws: WebSocket,
    role: Role,
    manager: ConnectionManager,
    listen_state: ListenState,
    anim_state: AnimState,
    drive: DriveController,
    camera_relay: CameraRelay | None = None,
    odom_hz: int = DEFAULT_ODOM_HZ,
    *,
    stt_runner: SttRunner | None = None,
    vad: VADService | None = None,
    speech_queue: SpeechQueue | None = None,
    sample_rate: int = 16000,
    vlm_qa: VlmQaService | None = None,
) -> None:
    if role not in ("device", "app", "media"):
        await ws.close(code=4000, reason="Invalid role")
        return

    if camera_relay is None:
        camera_relay = CameraRelay()

    # device / media の両ロールがバイナリ (マイク PCM / カメラ JPEG) を受け付ける
    # (device は --single-ws シミュレータ等との互換用)。app は受け付けない。
    accepts_binary = role in ("device", "media")

    # 音声認識はプラグインが off (none) でなく VAD があるときだけ回す。
    audio_enabled = accepts_binary and stt_runner is not None and vad is not None

    await manager.connect(ws, role)
    try:
        accumulator = vad.create_accumulator() if audio_enabled else None
        latest_audio: bytes | None = None
        audio_event = asyncio.Event()
        dropped_frames = 0

        def purge_audio() -> None:
            nonlocal accumulator, latest_audio, dropped_frames
            accumulator = vad.create_accumulator()
            latest_audio = None
            dropped_frames = 0
            audio_event.clear()

        if audio_enabled:
            # Register before the first await so the outer finally can always unregister.
            listen_state.register_purge(purge_audio)

        if role == "device":
            # listen → anim → drive_source → camera → odom_rate の順。
            await ws.send_text(ListenMessage(type="listen", on=listen_state.on).model_dump_json())
            await ws.send_text(AnimMessage(type="anim", mouth=anim_state.mouth).model_dump_json())
            await ws.send_text(DriveSourceMessage(type="drive_source", source=drive.source).model_dump_json())
            await ws.send_text(camera_relay.snapshot_message().model_dump_json())
            await ws.send_text(OdomRateMessage(type="odom_rate", hz=clamp_odom_hz(odom_hz)).model_dump_json())
        elif role == "app":
            await ws.send_text(
                StateMessage(type="state", value=f"listening:{'on' if listen_state.on else 'off'}").model_dump_json()
            )
            await ws.send_text(
                StateMessage(type="state", value=f"anim:mouth:{'on' if anim_state.mouth else 'off'}").model_dump_json()
            )
            await ws.send_text(StateMessage(type="state", value=f"drive_source:{drive.source}").model_dump_json())
            # STT のロード状態。
            stt_state = stt_runner.state if stt_runner is not None else "off"
            await ws.send_text(StateMessage(type="state", value=f"stt:{stt_state}").model_dump_json())
            # VLM の可用性と音声の入出力。
            if vlm_qa is not None:
                for state_msg in vlm_qa.state_messages():
                    await ws.send_text(state_msg.model_dump_json())
        # media: greeting なし。

        async def stt_worker() -> None:
            nonlocal latest_audio, dropped_frames
            assert stt_runner is not None
            while True:
                await audio_event.wait()
                audio_event.clear()
                audio = latest_audio
                latest_audio = None
                if audio is None:
                    continue
                if dropped_frames > 0:
                    logger.debug("Dropped %d stale audio frame(s)", dropped_frames)
                    dropped_frames = 0
                text = await stt_runner.transcribe(audio, sample_rate)
                if not listen_state.on:
                    continue
                if text.strip():
                    # heard フック (VLM が使う。既定は無しで intent は空)。
                    intent = await stt_runner.on_heard(text)
                    heard = HeardMessage(type="heard", text=text, intent=intent)
                    await manager.send_json_to_apps(heard)
                    await manager.send_json_to_devices(heard)
                    logger.info("STT result: %s", text[:100])

        stt_task = asyncio.create_task(stt_worker()) if audio_enabled else None
        try:
            while True:
                message = await ws.receive()

                if message["type"] == "websocket.disconnect":
                    break

                if "bytes" in message and message["bytes"]:
                    if not accepts_binary:
                        # app からのバイナリは使わない。
                        continue
                    try:
                        kind, _ts_ms, payload = decode_frame(message["bytes"])
                    except ValueError as e:
                        logger.warning("Invalid binary frame: %s", e)
                        continue
                    if kind == KIND_PCM:
                        if not audio_enabled or not stt_runner.accepts_audio or not listen_state.on:
                            continue
                        utterance = accumulator.feed(payload)
                        if utterance is not None:
                            if latest_audio is not None:
                                dropped_frames += 1
                            latest_audio = utterance
                            audio_event.set()
                    elif kind == KIND_JPEG:
                        # 最新フレームを保持し、ヘッダ付きのまま全 app へ中継する。
                        await camera_relay.handle_frame(message["bytes"], manager)
                    else:
                        logger.warning("Unknown binary frame kind: 0x%02X", kind)
                    continue

                if "text" in message and message["text"]:
                    if role == "media":
                        # media ロールはバイナリ専用。テキストは無視する。
                        logger.warning("Ignoring text on media role")
                        continue
                    raw = message["text"]
                    try:
                        msg = parse_message(raw)
                    except Exception as e:
                        logger.warning("Invalid message: %s", e)
                        continue

                    if isinstance(msg, SpeakMessage):
                        if speech_queue is None:
                            logger.warning("speak ignored (no speech queue): %s", msg.text[:50])
                        else:
                            await speech_queue.enqueue(msg.text, msg.emotion)

                    elif isinstance(msg, ControlMessage):
                        await handle_control(
                            msg.command,
                            msg.params,
                            manager=manager,
                            listen_state=listen_state,
                            anim_state=anim_state,
                            drive=drive,
                            camera_relay=camera_relay,
                            vlm_qa=vlm_qa,
                        )

                    elif isinstance(msg, VlmAskMessage):
                        if role != "app":
                            logger.warning("Ignoring vlm_ask from role=%s", role)
                        elif vlm_qa is None:
                            logger.warning("vlm_ask ignored (VLM QA is not configured): %s", msg.question[:50])
                        else:
                            await vlm_qa.submit(msg.question, "text")

                    elif isinstance(msg, OdomMessage | JoyMessage | EventMessage):
                        if role != "device":
                            logger.warning("Ignoring %s from role=%s", msg.type, role)
                            continue
                        # 再シリアライズせず、受信した生 JSON をそのまま全 app へ中継する。
                        await manager.send_to_apps(raw)
                        if isinstance(msg, OdomMessage):
                            drive.last_odom = json.loads(raw)
                        elif isinstance(msg, EventMessage):
                            logger.info("Device event: %s (%s)", msg.name, msg.detail)
                            if msg.name == "estop":
                                await drive.handle_estop(msg.detail)
        finally:
            if stt_task is not None:
                stt_task.cancel()
                try:
                    await stt_task
                except asyncio.CancelledError:
                    pass

    except WebSocketDisconnect:
        pass
    except Exception:
        logger.exception("WebSocket error for role=%s", role)
    finally:
        if audio_enabled:
            listen_state.unregister_purge(purge_audio)
        await manager.disconnect(ws, role)
