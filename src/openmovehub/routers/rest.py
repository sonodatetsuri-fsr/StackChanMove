"""REST API。

``/status``・``/drive``・``/control``・``/camera``・``/camera/latest.{jpg,json}``。
``/speak``・``/audio/plugins`` と ``/status.audio``。
``GET /`` → ``/web/`` のリダイレクトと ``/status.limits``。
``POST /vlm/qa``・``GET /vlm/frame/{id}``・``GET /vlm`` と ``/status.vlm``。
"""

import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response
from pydantic import BaseModel

from openmovehub.models.messages import ControlCommand
from openmovehub.plugins.registry import available_plugins
from openmovehub.routers.control import handle_control

logger = logging.getLogger(__name__)


class SpeakBody(BaseModel):
    """``POST /speak`` (WS の ``speak`` と同じ)。"""

    text: str
    emotion: str | None = None


class ControlBody(BaseModel):
    """``POST /control``。``command`` が ``ControlCommand`` に無ければ 422。"""

    command: ControlCommand
    params: dict[str, Any] = {}


class VlmQaBody(BaseModel):
    """``POST /vlm/qa`` (WS の ``vlm_ask`` と同じ)。"""

    question: str


#: ``POST /vlm/qa`` の ``error`` → HTTP ステータス (``error`` が null なら 200)。
VLM_ERROR_STATUS = {"unavailable": 503, "timeout": 504, "no_frame": 400, "invalid": 400, "busy": 429}


class DriveBody(BaseModel):
    v: float = 0.0
    w: float = 0.0


class CameraBody(BaseModel):
    """カメラ配信設定。省略したキーは現在値を保つ。"""

    fps: int | None = None
    quality: int | None = None
    on: bool | None = None
    auto: bool | None = None


async def _control(request: Request, command: str, params: dict[str, Any]) -> None:
    state = request.app.state
    await handle_control(
        command,
        params,
        manager=state.manager,
        listen_state=state.listen_state,
        anim_state=state.anim_state,
        drive=state.drive,
        camera_relay=state.camera_relay,
        vlm_qa=getattr(state, "vlm_qa", None),
    )


def vlm_status(state: Any) -> dict[str, Any]:
    """``GET /vlm`` と ``/status.vlm``。VLM QA が無い (テスト用の最小構成) ときは off / none。"""
    vlm_qa = getattr(state, "vlm_qa", None)
    if vlm_qa is None:
        return {
            "backend": "none",
            "available": False,
            "state": "off",
            "voice": {"input": False, "output": False},
            "queue": 0,
        }
    return vlm_qa.status()


def audio_status(state: Any) -> dict[str, Any]:
    """``/status.audio``。音声サービスが無い (テスト用の最小構成) ときは off / none。"""
    stt_runner = getattr(state, "stt_runner", None)
    tts_monitor = getattr(state, "tts_monitor", None)
    stt = stt_runner.status() if stt_runner is not None else {"plugin": "none", "state": "off"}
    tts = tts_monitor.status() if tts_monitor is not None else {"plugin": "none", "ok": False, "message": "disabled"}
    return {"stt": stt, "tts": tts}


def create_rest_router() -> APIRouter:
    router = APIRouter()

    @router.get("/", include_in_schema=False)
    async def root():
        """Web クライアントへ移る。"""
        return RedirectResponse(url="/web/")

    @router.post("/speak")
    async def speak(body: SpeakBody, request: Request):
        """発話を ``SpeechQueue`` に積む (TTS が none なら警告して捨てるが 200 を返す)。"""
        speech_queue = getattr(request.app.state, "speech_queue", None)
        if speech_queue is None:
            logger.warning("speak ignored (no speech queue): %s", body.text[:50])
        else:
            await speech_queue.enqueue(body.text, body.emotion)
        return {"status": "queued"}

    @router.get("/audio/plugins")
    async def audio_plugins(request: Request):
        """選べる STT / TTS プラグイン (組み込み + entry point) と現在の選択。"""
        audio = audio_status(request.app.state)
        return {
            "stt": available_plugins("stt"),
            "tts": available_plugins("tts"),
            "selected": {"stt": audio["stt"]["plugin"], "tts": audio["tts"]["plugin"]},
        }

    @router.post("/vlm/qa")
    async def vlm_qa(body: VlmQaBody, request: Request):
        """回答が出るまで待って ``{id, answer, ms, frame_seq, spoken, error}`` を返す (app への配信も行う)。"""
        service = getattr(request.app.state, "vlm_qa", None)
        if service is None:
            raise HTTPException(status_code=503, detail="vlm is not configured")
        answer = await service.ask(body.question, "text")
        content = answer.model_dump(include={"id", "answer", "ms", "frame_seq", "spoken", "error"})
        status_code = VLM_ERROR_STATUS.get(answer.error, 500) if answer.error is not None else 200
        return JSONResponse(status_code=status_code, content=content)

    @router.get("/vlm/frame/{qid}")
    async def vlm_frame(qid: int, request: Request):
        """その質問で使った JPEG (リングから消えていれば 404)。"""
        service = getattr(request.app.state, "vlm_qa", None)
        jpeg = service.frame(qid) if service is not None else None
        if jpeg is None:
            raise HTTPException(status_code=404, detail="no frame")
        return Response(content=jpeg, media_type="image/jpeg", headers={"Cache-Control": "no-store"})

    @router.get("/vlm")
    async def vlm(request: Request):
        """``VlmService.status()`` に ``state``・``voice{input,output}``・``queue`` を加えたもの。"""
        return vlm_status(request.app.state)

    @router.post("/control")
    async def control(body: ControlBody, request: Request):
        """WebSocket の ``control`` と同じ処理 (``handle_control``)。"""
        await _control(request, body.command, body.params)
        return {"status": "ok"}

    @router.post("/drive")
    async def drive(body: DriveBody, request: Request):
        """目標速度 (クランプ後を返す)。``drive_source=joystick`` 中は 409。"""
        controller = request.app.state.drive
        if controller.source == "joystick":
            raise HTTPException(status_code=409, detail="drive source is joystick")
        await controller.apply_app_command(body.v, body.w)
        v, w = controller.target
        return {"status": "ok", "target": {"v": v, "w": w}}

    @router.post("/camera")
    async def camera(body: CameraBody, request: Request):
        """カメラ配信設定 (``control camera`` と同じ実装を呼ぶ)。"""
        params = {k: v for k, v in body.model_dump().items() if v is not None}
        await _control(request, "camera", params)
        return {"status": "ok", "camera": request.app.state.camera_relay.status()}

    @router.get("/camera/latest.jpg")
    async def camera_latest_jpg(request: Request):
        """最新フレームを静止画として返す (未受信なら 404)。"""
        latest = request.app.state.camera_relay.latest
        if latest is None:
            raise HTTPException(status_code=404, detail="no frame")
        return Response(
            content=latest["jpeg"],
            media_type="image/jpeg",
            headers={"Cache-Control": "no-store"},
        )

    @router.get("/camera/latest.json")
    async def camera_latest_json(request: Request):
        """最新フレームのメタ情報 (未受信なら 404)。"""
        relay = request.app.state.camera_relay
        latest = relay.latest
        if latest is None:
            raise HTTPException(status_code=404, detail="no frame")
        return {
            "seq": latest["seq"],
            "device_ts_ms": latest["device_ts_ms"],
            "hub_ts": latest["hub_ts"],
            "size": len(latest["jpeg"]),
            "age_s": relay.last_frame_age_s,
        }

    @router.get("/status")
    async def status(request: Request):
        state = request.app.state
        controller = state.drive
        manager = state.manager
        snapshot = controller.snapshot()
        return {
            "drive_source": snapshot["drive_source"],
            "target": snapshot["target"],
            "devices": len(manager.devices),
            "media_connections": manager.media_count,
            "apps": len(manager.apps),
            "last_odom": controller.last_odom,
            "limits": controller.limits(),
            "camera": state.camera_relay.status(),
            "listen": state.listen_state.on,
            "anim": {"mouth": state.anim_state.mouth},
            "audio": audio_status(state),
            "vlm": vlm_status(state),
        }

    return router
