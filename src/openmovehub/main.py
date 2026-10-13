"""Open StackChan Move Hub (FastAPI アプリ)。

起動: ``uv run uvicorn openmovehub.main:app --host 0.0.0.0 --port 8341``
"""

import logging
import os
import pathlib
from contextlib import asynccontextmanager
from typing import Literal

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from starlette.websockets import WebSocket

from openmovehub.config import DEPRECATION_WARNINGS, settings
from openmovehub.models.messages import StateMessage
from openmovehub.plugins.registry import load_plugin
from openmovehub.routers.rest import create_rest_router
from openmovehub.routers.ws import websocket_endpoint
from openmovehub.services.anim_state import AnimState
from openmovehub.services.camera_relay import CameraRelay
from openmovehub.services.connection_manager import ConnectionManager
from openmovehub.services.drive_controller import DriveController
from openmovehub.services.listen_state import ListenState
from openmovehub.services.speech_queue import SpeechQueue
from openmovehub.services.stt_runner import SttRunner, load_ng_words
from openmovehub.services.tts_monitor import TtsMonitor
from openmovehub.services.vad import VADService
from openmovehub.services.vlm import VlmService
from openmovehub.services.vlm_qa import STT_REJECT_STATES, VlmQaService, log_vlm_state

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)
# TTS と VLM の probe (30 s ごと) の HTTP リクエストを毎回 INFO で出さない。
logging.getLogger("httpx").setLevel(logging.WARNING)

Role = Literal["device", "app", "media"]


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Starting hub on %s:%d", settings.host, settings.port)

    manager = ConnectionManager(send_timeout_s=settings.ws_send_timeout_s)
    listen_state = ListenState(on=settings.listen_default)
    anim_state = AnimState(mouth=settings.anim_mouth_default)
    drive = DriveController(manager, settings)
    drive.start()
    camera_relay = CameraRelay(
        fps=settings.camera_fps,
        auto=settings.camera_fps_auto,
        fps_min=settings.camera_fps_min,
        fps_max=settings.camera_fps_max,
    )
    camera_relay.start(manager)
    logger.info(
        "Camera relay ready (fps=%d quality=%d auto=%s range=%d..%d)",
        camera_relay.fps,
        camera_relay.quality,
        camera_relay.auto,
        camera_relay.fps_min,
        camera_relay.fps_max,
    )
    logger.info("Odom rate for devices: %d Hz", settings.odom_hz)

    # 音声 (STT / TTS プラグイン、VAD、発話キュー)。
    for warning in DEPRECATION_WARNINGS:
        logger.warning(warning)

    async def notify_stt_state(state: str) -> None:
        await manager.send_json_to_apps(StateMessage(type="state", value=f"stt:{state}"))

    stt_loaded = load_plugin("stt", settings.stt_plugin, settings)
    stt_runner = SttRunner(
        stt_loaded.plugin,
        label=stt_loaded.label,
        ng_words=load_ng_words(settings.stt_ng_words_path),
        notify=notify_stt_state,
    )
    stt_runner.start()
    vad = VADService(settings)
    tts_loaded = load_plugin("tts", settings.tts_plugin, settings)
    tts_monitor = TtsMonitor(tts_loaded.plugin, label=tts_loaded.label, interval_s=settings.tts_probe_interval_s)
    tts_monitor.start()
    speech_queue = SpeechQueue(tts_loaded.plugin, manager, settings, listen_state)
    speech_queue.start()

    # VLM (Ollama) と質問応答。Ollama が起動していなくても Hub は起動する。
    raw_vlm_backend = os.getenv("VLM_BACKEND", "").strip().lower()
    if raw_vlm_backend and raw_vlm_backend not in ("ollama", "none"):
        logger.warning("VLM_BACKEND=%s is not supported; VLM disabled", raw_vlm_backend)
    vlm = VlmService(settings)
    if vlm.backend is not None:
        await vlm.refresh(force=True)
    log_vlm_state(vlm)
    vlm_qa = VlmQaService(
        vlm,
        manager,
        camera_relay,
        settings,
        speech_queue=speech_queue,
        tts_available=lambda: bool(tts_monitor.status().get("ok")),
        stt_state=lambda: stt_runner.state,
    )
    # 既定で音声入力 ON のときも、STT が無ければ OFF、listen は ON に揃える。
    if vlm_qa.voice.input:
        if stt_runner.state in STT_REJECT_STATES:
            logger.warning("VLM_VOICE_INPUT_DEFAULT=on ignored (stt=%s)", stt_runner.state)
            vlm_qa.voice.input = False
        elif not listen_state.on:
            logger.info("VLM voice input is on; listen flag set to True")
            listen_state.on = True
    stt_runner.heard_hook = vlm_qa.on_heard
    vlm_qa.start()

    app.state.manager = manager
    app.state.listen_state = listen_state
    app.state.anim_state = anim_state
    app.state.drive = drive
    app.state.camera_relay = camera_relay
    app.state.stt_runner = stt_runner
    app.state.vad = vad
    app.state.tts_monitor = tts_monitor
    app.state.speech_queue = speech_queue
    app.state.vlm = vlm
    app.state.vlm_qa = vlm_qa

    logger.info("Hub started")
    yield

    await vlm_qa.stop()
    await vlm.close()
    await speech_queue.stop()
    await tts_monitor.stop()
    await tts_loaded.plugin.close()
    stt_runner.close()
    await drive.stop()
    await camera_relay.stop()
    await camera_relay.wait_idle()
    logger.info("Hub stopped")


app = FastAPI(title="Open StackChan Move Hub", lifespan=lifespan)
app.include_router(create_rest_router())

# Web クライアント の置き場。ディレクトリがあるときだけマウントする。
web_dir = pathlib.Path(__file__).resolve().parent.parent.parent / "web"
if web_dir.is_dir():
    app.mount("/web", StaticFiles(directory=str(web_dir), html=True), name="web")


@app.websocket("/ws/{role}")
async def ws_endpoint(ws: WebSocket, role: Role):
    await websocket_endpoint(
        ws=ws,
        role=role,
        manager=app.state.manager,
        listen_state=app.state.listen_state,
        anim_state=app.state.anim_state,
        drive=app.state.drive,
        camera_relay=app.state.camera_relay,
        odom_hz=settings.odom_hz,
        stt_runner=app.state.stt_runner,
        vad=app.state.vad,
        speech_queue=app.state.speech_queue,
        sample_rate=settings.sample_rate,
        vlm_qa=app.state.vlm_qa,
    )
