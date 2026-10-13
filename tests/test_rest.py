"""REST API。"""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from openmovehub.config import Settings
from openmovehub.models.frames import KIND_JPEG, encode_frame
from openmovehub.routers.rest import create_rest_router
from openmovehub.services.anim_state import AnimState
from openmovehub.services.camera_relay import CameraRelay
from openmovehub.services.drive_controller import DriveController
from openmovehub.services.listen_state import ListenState
from openmovehub.services.vlm import VlmResult, VlmService, VlmTimeout, VlmUnavailable
from openmovehub.services.vlm_qa import VlmQaService


def make_mock_manager(devices: int = 0, apps: int = 0, media: int = 0):
    manager = MagicMock()
    manager.send_json_to_devices = AsyncMock()
    manager.send_json_to_apps = AsyncMock()
    manager.send_to_devices = AsyncMock()
    manager.send_to_apps = AsyncMock()
    manager.devices = [MagicMock() for _ in range(devices)]
    manager.apps = [MagicMock() for _ in range(apps)]
    manager.media = [MagicMock() for _ in range(media)]
    manager.media_count = media
    return manager


def build_test_app(mock_manager, settings=None):
    settings = settings or Settings()
    app = FastAPI()
    app.include_router(create_rest_router())
    app.state.manager = mock_manager
    app.state.listen_state = ListenState(on=settings.listen_default)
    app.state.anim_state = AnimState(mouth=settings.anim_mouth_default)
    app.state.drive = DriveController(mock_manager, settings)
    app.state.camera_relay = CameraRelay(
        fps=settings.camera_fps,
        auto=settings.camera_fps_auto,
        fps_min=settings.camera_fps_min,
        fps_max=settings.camera_fps_max,
    )
    return app


def client(app):
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


# --- /control ----------------------------------------------------------------


async def test_post_control_face_returns_200():
    app = build_test_app(make_mock_manager())
    async with client(app) as c:
        response = await c.post("/control", json={"command": "face", "params": {"expr": "happy"}})
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


async def test_post_control_face_sends_to_devices():
    manager = make_mock_manager()
    app = build_test_app(manager)
    async with client(app) as c:
        await c.post("/control", json={"command": "face", "params": {"expr": "sad"}})
    manager.send_json_to_devices.assert_awaited_once()
    sent_msg = manager.send_json_to_devices.await_args[0][0]
    assert sent_msg.type == "face"
    assert sent_msg.expr == "sad"


async def test_post_control_listen_off_updates_state_and_broadcasts():
    manager = make_mock_manager()
    app = build_test_app(manager)
    async with client(app) as c:
        response = await c.post("/control", json={"command": "listen", "params": {"on": False}})
    assert response.status_code == 200
    assert app.state.listen_state.on is False
    assert manager.send_json_to_devices.await_args[0][0].on is False
    assert manager.send_json_to_apps.await_args[0][0].value == "listening:off"


async def test_post_control_anim_mouth_off_updates_state_and_broadcasts():
    manager = make_mock_manager()
    app = build_test_app(manager)
    async with client(app) as c:
        await c.post("/control", json={"command": "anim", "params": {"mouth": False}})
    assert app.state.anim_state.mouth is False
    assert manager.send_json_to_devices.await_args[0][0].mouth is False
    assert manager.send_json_to_apps.await_args[0][0].value == "anim:mouth:off"


async def test_post_control_drive_source_switches_and_broadcasts():
    manager = make_mock_manager()
    app = build_test_app(manager)
    async with client(app) as c:
        response = await c.post("/control", json={"command": "drive_source", "params": {"source": "joystick"}})
    assert response.status_code == 200
    assert app.state.drive.source == "joystick"
    device_msg = manager.send_json_to_devices.await_args[0][0]
    assert device_msg.type == "drive_source"
    assert device_msg.source == "joystick"
    app_msg = manager.send_json_to_apps.await_args[0][0]
    assert app_msg.value == "drive_source:joystick"


@pytest.mark.parametrize("initial", ["hub", "joystick"])
async def test_post_control_drive_source_auto_is_rejected(initial):
    """``auto`` は拒否し、``state drive_source:<現在値>`` を返す。"""
    manager = make_mock_manager()
    app = build_test_app(manager, Settings(drive_source_default=initial))
    async with client(app) as c:
        response = await c.post("/control", json={"command": "drive_source", "params": {"source": "auto"}})
    assert response.status_code == 200
    assert app.state.drive.source == initial
    manager.send_json_to_devices.assert_not_awaited()
    manager.send_json_to_apps.assert_awaited_once()
    assert manager.send_json_to_apps.await_args[0][0].value == f"drive_source:{initial}"


async def test_post_control_drive_sets_target():
    app = build_test_app(make_mock_manager())
    async with client(app) as c:
        response = await c.post("/control", json={"command": "drive", "params": {"v": 0.1, "w": 0.5}})
    assert response.status_code == 200
    assert app.state.drive.target == (0.1, 0.5)


async def test_post_control_odom_reset_sends_to_devices():
    manager = make_mock_manager()
    app = build_test_app(manager)
    async with client(app) as c:
        await c.post("/control", json={"command": "odom_reset", "params": {}})
    sent_msg = manager.send_json_to_devices.await_args[0][0]
    assert sent_msg.type == "odom_reset"


async def test_post_control_params_default_to_empty():
    manager = make_mock_manager()
    app = build_test_app(manager)
    async with client(app) as c:
        response = await c.post("/control", json={"command": "odom_reset"})
    assert response.status_code == 200
    assert manager.send_json_to_devices.await_args[0][0].type == "odom_reset"


@pytest.mark.parametrize("command", ["record", "explore", "odom_rate", "wheel_pwm", "train", "pose", "wiggle"])
async def test_post_control_removed_or_unknown_command_is_422(command):
    """``control.command`` の Literal に無いコマンドは検証で拒否する。"""
    manager = make_mock_manager()
    app = build_test_app(manager)
    async with client(app) as c:
        response = await c.post("/control", json={"command": command, "params": {"on": True}})
    assert response.status_code == 422
    manager.send_json_to_devices.assert_not_awaited()
    manager.send_json_to_apps.assert_not_awaited()


# --- /drive ------------------------------------------------------------------


async def test_post_drive_returns_target():
    app = build_test_app(make_mock_manager())
    async with client(app) as c:
        response = await c.post("/drive", json={"v": 0.1, "w": -0.5})
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "target": {"v": 0.1, "w": -0.5}}
    assert app.state.drive.target == (0.1, -0.5)


async def test_post_drive_clamps_to_limits():
    settings = Settings(max_v=0.15, max_w=2.0)
    app = build_test_app(make_mock_manager(), settings)
    async with client(app) as c:
        response = await c.post("/drive", json={"v": 9.0, "w": -9.0})
    assert response.json()["target"] == {"v": 0.15, "w": -2.0}


async def test_post_drive_defaults_to_zero():
    app = build_test_app(make_mock_manager())
    async with client(app) as c:
        response = await c.post("/drive", json={})
    assert response.json()["target"] == {"v": 0.0, "w": 0.0}


async def test_post_drive_409_while_joystick():
    """``drive_source=joystick`` 中は 409。目標は変えない。"""
    app = build_test_app(make_mock_manager())
    async with client(app) as c:
        await c.post("/control", json={"command": "drive_source", "params": {"source": "joystick"}})
        response = await c.post("/drive", json={"v": 0.1, "w": 0.0})
    assert response.status_code == 409
    assert response.json() == {"detail": "drive source is joystick"}
    assert app.state.drive.target == (0.0, 0.0)


async def test_post_drive_accepted_again_after_returning_to_hub():
    app = build_test_app(make_mock_manager(), Settings(drive_source_default="joystick"))
    async with client(app) as c:
        assert (await c.post("/drive", json={"v": 0.1})).status_code == 409
        await c.post("/control", json={"command": "drive_source", "params": {"source": "hub"}})
        response = await c.post("/drive", json={"v": 0.1})
    assert response.status_code == 200
    assert response.json()["target"] == {"v": 0.1, "w": 0.0}


# --- /status -----------------------------------------------------------------


async def test_get_status_shape():
    manager = make_mock_manager(devices=1, apps=2)
    app = build_test_app(manager)
    app.state.drive.last_odom = {"type": "odom", "x": 1.0}
    async with client(app) as c:
        response = await c.get("/status")
    assert response.status_code == 200
    body = response.json()
    assert set(body) == {
        "drive_source",
        "target",
        "devices",
        "media_connections",
        "apps",
        "last_odom",
        "limits",
        "camera",
        "listen",
        "anim",
        "audio",
        "vlm",
    }
    camera = body.pop("camera")
    # /status のキー (実装はこれに内部状態を足す)。
    assert {"on", "fps", "quality", "auto", "fps_measured", "last_frame_age_s"} <= set(camera)
    assert (camera["on"], camera["fps"], camera["quality"], camera["auto"]) == (True, 4, 60, True)
    assert camera["fps_measured"] == 0.0
    assert camera["last_frame_age_s"] is None
    assert body == {
        "drive_source": "hub",
        "target": {"v": 0.0, "w": 0.0},
        "devices": 1,
        "media_connections": 0,
        "apps": 2,
        "last_odom": {"type": "odom", "x": 1.0},
        "limits": {"max_v": 0.15, "max_w": 2.0},
        "listen": True,
        "anim": {"mouth": True},
        # 音声サービスを載せない最小構成では off / none。
        "audio": {
            "stt": {"plugin": "none", "state": "off"},
            "tts": {"plugin": "none", "ok": False, "message": "disabled"},
        },
        # VLM を載せない最小構成では off。
        "vlm": {
            "backend": "none",
            "available": False,
            "state": "off",
            "voice": {"input": False, "output": False},
            "queue": 0,
        },
    }


async def test_get_status_reflects_media_connections():
    manager = make_mock_manager(devices=1, apps=1, media=2)
    app = build_test_app(manager)
    async with client(app) as c:
        body = (await c.get("/status")).json()
    assert body["media_connections"] == 2


async def test_get_status_reflects_drive():
    app = build_test_app(make_mock_manager())
    async with client(app) as c:
        await c.post("/drive", json={"v": 0.05, "w": 1.0})
        body = (await c.get("/status")).json()
    assert body["target"] == {"v": 0.05, "w": 1.0}
    assert body["last_odom"] is None


async def test_get_status_reflects_joystick_listen_and_anim():
    app = build_test_app(make_mock_manager())
    async with client(app) as c:
        await c.post("/control", json={"command": "drive_source", "params": {"source": "joystick"}})
        await c.post("/control", json={"command": "listen", "params": {"on": False}})
        await c.post("/control", json={"command": "anim", "params": {"mouth": False}})
        body = (await c.get("/status")).json()
    assert body["drive_source"] == "joystick"
    assert body["listen"] is False
    assert body["anim"] == {"mouth": False}


# --- camera ------------------------------------------------------------------

JPEG = b"\xff\xd8\xff\xe0" + b"jpeg-body" * 8


async def _feed_frame(app, ts_ms: int = 5, payload: bytes = JPEG):
    await app.state.camera_relay.handle_frame(encode_frame(KIND_JPEG, payload, ts_ms=ts_ms), app.state.manager)


async def test_get_camera_latest_jpg_404_then_200():
    app = build_test_app(make_mock_manager())
    async with client(app) as c:
        missing = await c.get("/camera/latest.jpg")
        assert missing.status_code == 404
        assert missing.json() == {"detail": "no frame"}

        await _feed_frame(app)
        response = await c.get("/camera/latest.jpg")

    assert response.status_code == 200
    assert response.headers["content-type"] == "image/jpeg"
    assert response.headers["cache-control"] == "no-store"
    assert response.content == JPEG


async def test_get_camera_latest_json_404_then_fields():
    app = build_test_app(make_mock_manager())
    async with client(app) as c:
        assert (await c.get("/camera/latest.json")).status_code == 404

        await _feed_frame(app, ts_ms=1234)
        body = (await c.get("/camera/latest.json")).json()

    assert body["seq"] == 1
    assert body["device_ts_ms"] == 1234
    assert body["size"] == len(JPEG)
    assert isinstance(body["hub_ts"], float)
    assert body["age_s"] >= 0.0


async def test_get_status_includes_camera_after_frame():
    app = build_test_app(make_mock_manager())
    async with client(app) as c:
        await _feed_frame(app)
        camera = (await c.get("/status")).json()["camera"]

    assert camera["seq"] == 1
    assert camera["size"] == len(JPEG)
    assert camera["fps_measured"] == 0.5
    assert camera["last_frame_age_s"] >= 0.0
    assert camera["on"] is True


async def test_post_control_camera_forwards_snapshot_to_devices():
    manager = make_mock_manager()
    app = build_test_app(manager)
    async with client(app) as c:
        response = await c.post(
            "/control", json={"command": "camera", "params": {"fps": 99, "on": False, "auto": False}}
        )

    assert response.status_code == 200
    sent = manager.send_json_to_devices.await_args[0][0]
    assert (sent.type, sent.on, sent.fps, sent.quality) == ("camera", False, 10, 60)
    assert app.state.camera_relay.fps == 10


async def test_post_control_camera_fps_in_auto_mode_updates_the_max():
    """自動制御中の ``fps`` は上限 (``fps_max``) の更新として扱う。"""
    app = build_test_app(make_mock_manager())
    relay = app.state.camera_relay
    async with client(app) as c:
        await c.post("/control", json={"command": "camera", "params": {"fps": 3}})
    assert (relay.auto, relay.fps_max, relay.fps) == (True, 3, 3)


async def test_post_camera_manual_fixes_the_fps():
    """``POST /camera {auto:false, fps:6}`` で自動を止めて 6 fps に固定する。"""
    manager = make_mock_manager()
    app = build_test_app(manager)
    async with client(app) as c:
        response = await c.post("/camera", json={"auto": False, "fps": 6})

    assert response.status_code == 200
    camera = response.json()["camera"]
    assert (camera["fps"], camera["auto"]) == (6, False)
    sent = manager.send_json_to_devices.await_args[0][0]
    assert (sent.type, sent.fps) == ("camera", 6)
    assert app.state.camera_relay.auto is False


async def test_post_camera_omitted_fields_keep_current_values():
    app = build_test_app(make_mock_manager())
    app.state.camera_relay.apply_control({"auto": False, "fps": 7, "quality": 40})
    async with client(app) as c:
        camera = (await c.post("/camera", json={"on": False})).json()["camera"]
    assert (camera["on"], camera["fps"], camera["quality"], camera["auto"]) == (False, 7, 40, False)


# --- 削除したエンドポイント ------------------------------------------------------


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("post", "/record"),
        ("post", "/wheel_pwm"),
        ("post", "/odom_rate"),
        ("post", "/autonomy"),
        ("post", "/explore"),
        # stackchan_move の ``POST /vlm/ask`` は持たない (質問応答は ``POST /vlm/qa``)。
        ("post", "/vlm/ask"),
        ("get", "/pose"),
    ],
)
async def test_removed_endpoints_are_404(method, path):
    app = build_test_app(make_mock_manager())
    async with client(app) as c:
        response = await getattr(c, method)(path, **({"json": {}} if method == "post" else {}))
    assert response.status_code in (404, 405)


# --- GET / と /status.limits ------------------------------------------


async def test_root_redirects_to_web():
    app = build_test_app(make_mock_manager())
    async with client(app) as c:
        response = await c.get("/", follow_redirects=False)
    assert response.status_code in (302, 307)
    assert response.headers["location"] == "/web/"


async def test_status_limits_follow_settings():
    app = build_test_app(make_mock_manager(), Settings(max_v=0.2, max_w=1.5))
    async with client(app) as c:
        body = (await c.get("/status")).json()
    assert body["limits"] == {"max_v": 0.2, "max_w": 1.5}


# --- POST /vlm/qa・GET /vlm/frame/{id}・GET /vlm ---------------------------


class _QaBackend:
    """``VlmService`` 用のダミーバックエンド (``chat`` の結果・例外を差し替えられる)。"""

    name = "fake"
    url = "http://fake"
    model = "fake:1b"

    def __init__(self, fail: Exception | None = None, text: str = "机の上にコップがあります。") -> None:
        self.fail = fail
        self.text = text
        self.gate: asyncio.Event | None = None

    async def probe(self) -> dict:
        return {"ok": True, "version": "0.33.1", "model_present": True, "message": "ok"}

    async def chat(self, images, system, user, schema, options) -> VlmResult:
        if self.gate is not None:
            await self.gate.wait()
        if self.fail is not None:
            raise self.fail
        return VlmResult(text=self.text, data=None, ms=12.5, load_ms=0.0, prompt_tokens=1, eval_tokens=1, model="m")

    async def aclose(self) -> None:
        return None


async def build_vlm_app(backend=None, *, frame: bool = True, settings=None):
    settings = settings or Settings()
    manager = make_mock_manager()
    app = build_test_app(manager, settings)
    vlm = VlmService(settings, backend=backend or _QaBackend())
    await vlm.refresh(force=True)
    qa = VlmQaService(vlm, manager, app.state.camera_relay, settings)
    qa.start()
    app.state.vlm_qa = qa
    if frame:
        await app.state.camera_relay.handle_frame(encode_frame(KIND_JPEG, JPEG, 5), manager)
    return app, qa


async def test_post_vlm_qa_200_returns_answer_and_frame():
    app, qa = await build_vlm_app()
    try:
        async with client(app) as c:
            response = await c.post("/vlm/qa", json={"question": "何が見えますか?"})
            assert response.status_code == 200
            body = response.json()
            assert body == {
                "id": 1,
                "answer": "机の上にコップがあります。",
                "ms": 12.5,
                "frame_seq": 1,
                "spoken": False,
                "error": None,
            }
            frame = await c.get("/vlm/frame/1")
            assert frame.status_code == 200
            assert frame.headers["content-type"] == "image/jpeg"
            assert frame.content == JPEG
            assert (await c.get("/vlm/frame/99")).status_code == 404
    finally:
        await qa.stop()


@pytest.mark.parametrize(
    ("backend", "frame", "question", "status", "error"),
    [
        (_QaBackend(fail=VlmUnavailable("down")), True, "q", 503, "unavailable"),
        (_QaBackend(fail=VlmTimeout("slow")), True, "q", 504, "timeout"),
        (None, False, "q", 400, "no_frame"),
        (None, True, "   ", 400, "invalid"),
        (None, True, "あ" * 501, 400, "invalid"),
    ],
)
async def test_post_vlm_qa_error_status_codes(backend, frame, question, status, error):
    app, qa = await build_vlm_app(backend, frame=frame)
    try:
        async with client(app) as c:
            response = await c.post("/vlm/qa", json={"question": question})
        assert response.status_code == status
        body = response.json()
        assert body["error"] == error
        assert body["answer"] == ""
        assert body["ms"] is None
    finally:
        await qa.stop()


async def test_post_vlm_qa_busy_is_429():
    backend = _QaBackend()
    backend.gate = asyncio.Event()
    app, qa = await build_vlm_app(backend, settings=Settings(vlm_qa_queue_max=1))
    try:
        async with client(app) as c:
            first = asyncio.create_task(c.post("/vlm/qa", json={"question": "1"}))
            await asyncio.sleep(0.05)  # 1 件目が処理中 (gate で止まる)
            second = asyncio.create_task(c.post("/vlm/qa", json={"question": "2"}))
            await asyncio.sleep(0.05)  # 2 件目が待ち行列に入る (上限 1)
            third = await c.post("/vlm/qa", json={"question": "3"})
            assert third.status_code == 429
            assert third.json()["error"] == "busy"
            backend.gate.set()
            assert (await first).status_code == 200
            assert (await second).status_code == 200
    finally:
        await qa.stop()


async def test_post_vlm_qa_without_service_is_503():
    app = build_test_app(make_mock_manager())
    async with client(app) as c:
        response = await c.post("/vlm/qa", json={"question": "q"})
        assert response.status_code == 503
        assert (await c.get("/vlm/frame/1")).status_code == 404


async def test_get_vlm_and_status_vlm():
    app, qa = await build_vlm_app()
    try:
        async with client(app) as c:
            body = (await c.get("/vlm")).json()
            status = (await c.get("/status")).json()
        assert body["available"] is True
        assert body["state"] == "ready"
        assert body["model"] == "fake:1b"
        assert body["voice"] == {"input": False, "output": False}
        assert body["queue"] == 0
        assert status["vlm"]["state"] == "ready"
    finally:
        await qa.stop()
