"""音声の入出力の切替 (``control vlm_voice``)・heard フック・listen との連動。"""

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from openmovehub.config import Settings
from openmovehub.models.frames import KIND_JPEG, KIND_PCM, encode_frame
from openmovehub.models.messages import ControlMessage, parse_message
from openmovehub.routers.control import handle_control
from openmovehub.routers.ws import websocket_endpoint
from openmovehub.services.anim_state import AnimState
from openmovehub.services.camera_relay import CameraRelay
from openmovehub.services.connection_manager import ConnectionManager
from openmovehub.services.drive_controller import DriveController
from openmovehub.services.listen_state import ListenState
from openmovehub.services.stt_runner import SttRunner
from openmovehub.services.vlm import VlmResult, VlmService
from openmovehub.services.vlm_qa import VlmQaService, VlmVoiceState

JPEG = b"\xff\xd8\xff\xe0" + b"photo" * 8


class FakeBackend:
    name = "fake"
    url = "http://fake"
    model = "fake:1b"

    async def probe(self) -> dict:
        return {"ok": True, "version": "0.33.1", "model_present": True, "message": "ok"}

    async def chat(self, images, system, user, schema, options) -> VlmResult:
        return VlmResult(
            text="コップです。", data=None, ms=10.0, load_ms=0.0, prompt_tokens=1, eval_tokens=1, model="m"
        )

    async def aclose(self) -> None:
        return None


def make_ws(messages: list[dict] | None = None) -> MagicMock:
    ws = MagicMock()
    ws.accept = AsyncMock()
    ws.send_text = AsyncMock()
    ws.send_bytes = AsyncMock()
    ws.close = AsyncMock()
    ws.receive = AsyncMock(side_effect=list(messages or []) + [{"type": "websocket.disconnect"}])
    return ws


def sent(ws: MagicMock) -> list[dict]:
    return [json.loads(c.args[0]) for c in ws.send_text.await_args_list]


def states(ws: MagicMock) -> list[str]:
    return [m["value"] for m in sent(ws) if m["type"] == "state"]


class Ctx:
    def __init__(self, stt: str = "ready", listen: bool = True, voice_in: bool = False) -> None:
        self.settings = Settings()
        self.manager = ConnectionManager()
        self.listen_state = ListenState(on=listen)
        self.anim_state = AnimState(mouth=True)
        self.drive = DriveController(self.manager, self.settings)
        self.relay = CameraRelay()
        self.stt = stt
        self.vlm = VlmService(self.settings, backend=FakeBackend())
        self.qa = VlmQaService(
            self.vlm,
            self.manager,
            self.relay,
            self.settings,
            stt_state=lambda: self.stt,
            voice=VlmVoiceState(input=voice_in, output=False),
        )

    async def connect_app(self) -> MagicMock:
        ws = make_ws()
        await self.manager.connect(ws, "app")
        return ws

    async def connect_device(self) -> MagicMock:
        ws = make_ws()
        await self.manager.connect(ws, "device")
        return ws

    async def control(self, command: str, params: dict) -> None:
        await handle_control(
            command,
            params,
            manager=self.manager,
            listen_state=self.listen_state,
            anim_state=self.anim_state,
            drive=self.drive,
            camera_relay=self.relay,
            vlm_qa=self.qa,
        )


def test_control_message_vlm_voice_parses():
    msg = parse_message(json.dumps({"type": "control", "command": "vlm_voice", "params": {"input": True}}))
    assert isinstance(msg, ControlMessage)
    assert msg.command == "vlm_voice"


def test_settings_voice_defaults_are_off():
    s = Settings()
    assert (s.vlm_voice_input_default, s.vlm_voice_output_default) == (False, False)


def test_voice_state_defaults_follow_settings():
    settings = Settings(vlm_voice_input_default=True, vlm_voice_output_default=True)
    qa = VlmQaService(VlmService(settings, backend=FakeBackend()), ConnectionManager(), CameraRelay(), settings)
    assert (qa.voice.input, qa.voice.output) == (True, True)


# --- 切替と state ------------------------------------------------------------------


async def test_voice_output_toggle_broadcasts_on_change_only():
    ctx = Ctx()
    app = await ctx.connect_app()
    await ctx.control("vlm_voice", {"output": True})
    await ctx.control("vlm_voice", {"output": True})
    await ctx.control("vlm_voice", {"output": False})
    assert states(app) == ["vlm_voice_out:on", "vlm_voice_out:off"]
    assert ctx.qa.voice.output is False


async def test_voice_input_on_turns_listen_on():
    ctx = Ctx(listen=False)
    app = await ctx.connect_app()
    device = await ctx.connect_device()
    await ctx.control("vlm_voice", {"input": True})
    assert ctx.qa.voice.input is True
    assert ctx.listen_state.on is True
    assert states(app) == ["vlm_voice_in:on", "listening:on"]
    assert {"type": "listen", "on": True} in sent(device)


async def test_voice_input_on_with_listen_already_on_does_not_resend_listen():
    ctx = Ctx(listen=True)
    app = await ctx.connect_app()
    await ctx.control("vlm_voice", {"input": True})
    assert states(app) == ["vlm_voice_in:on"]


async def test_listen_off_turns_voice_input_off():
    ctx = Ctx(listen=True, voice_in=True)
    app = await ctx.connect_app()
    await ctx.control("listen", {"on": False})
    assert ctx.listen_state.on is False
    assert ctx.qa.voice.input is False
    assert states(app) == ["listening:off", "vlm_voice_in:off"]


async def test_listen_on_does_not_turn_voice_input_on():
    ctx = Ctx(listen=False)
    await ctx.control("listen", {"on": True})
    assert ctx.qa.voice.input is False


async def test_voice_input_off_keeps_listen():
    ctx = Ctx(listen=True, voice_in=True)
    await ctx.control("vlm_voice", {"input": False})
    assert ctx.qa.voice.input is False
    assert ctx.listen_state.on is True


@pytest.mark.parametrize("stt", ["off", "error"])
async def test_voice_input_rejected_when_stt_off_or_error(stt):
    ctx = Ctx(stt=stt, listen=False)
    app = await ctx.connect_app()
    await ctx.control("vlm_voice", {"input": True})
    assert ctx.qa.voice.input is False
    assert ctx.listen_state.on is False
    assert states(app) == ["vlm_voice_in:off"]


async def test_voice_input_accepted_while_stt_loading():
    ctx = Ctx(stt="loading", listen=False)
    app = await ctx.connect_app()
    await ctx.control("vlm_voice", {"input": True})
    assert ctx.qa.voice.input is True
    assert ctx.listen_state.on is True
    assert states(app) == ["vlm_voice_in:on", "listening:on"]


async def test_input_and_output_in_one_command():
    ctx = Ctx()
    await ctx.control("vlm_voice", {"input": True, "output": True})
    assert (ctx.qa.voice.input, ctx.qa.voice.output) == (True, True)


async def test_vlm_voice_without_service_is_ignored():
    ctx = Ctx()
    await handle_control(
        "vlm_voice",
        {"input": True},
        manager=ctx.manager,
        listen_state=ctx.listen_state,
        anim_state=ctx.anim_state,
        drive=ctx.drive,
    )


# --- heard フック -----------------------------------------------------------------------


async def test_heard_hook_submits_voice_question_when_input_on():
    ctx = Ctx(voice_in=True)
    ctx.qa.submit = AsyncMock(return_value=1)
    assert await ctx.qa.on_heard("目の前に何がありますか") == "vlm"
    ctx.qa.submit.assert_awaited_once_with("目の前に何がありますか", "voice")


async def test_heard_hook_does_nothing_when_input_off():
    ctx = Ctx(voice_in=False)
    ctx.qa.submit = AsyncMock(return_value=1)
    assert await ctx.qa.on_heard("目の前に何がありますか") == ""
    ctx.qa.submit.assert_not_awaited()


def _stt_runner(text: str) -> SttRunner:
    plugin = MagicMock()
    plugin.name = "mock"
    plugin.transcribe = MagicMock(return_value=text)
    plugin.status = MagicMock(return_value={})
    runner = SttRunner(plugin, label="mock")
    runner._state = "ready"
    return runner


def _vad() -> MagicMock:
    vad = MagicMock()
    accumulator = MagicMock()
    accumulator.feed = MagicMock(side_effect=lambda chunk: chunk)
    vad.create_accumulator = MagicMock(return_value=accumulator)
    return vad


async def _speak_into_media(ctx: Ctx, runner: SttRunner) -> None:
    frames = [{"type": "websocket.bytes", "bytes": encode_frame(KIND_PCM, b"\x00\x01" * 100)}]

    async def receive():
        if frames:
            return frames.pop(0)
        await asyncio.sleep(0.1)
        return {"type": "websocket.disconnect"}

    media = make_ws()
    media.receive = AsyncMock(side_effect=receive)
    await websocket_endpoint(
        media,
        "media",
        ctx.manager,
        ctx.listen_state,
        ctx.anim_state,
        ctx.drive,
        ctx.relay,
        stt_runner=runner,
        vad=_vad(),
        vlm_qa=ctx.qa,
    )


@pytest.mark.parametrize(("voice_in", "intent"), [(True, "vlm"), (False, "")])
async def test_heard_through_ws_marks_intent_and_asks_vlm(voice_in, intent):
    ctx = Ctx(voice_in=voice_in)
    app = await ctx.connect_app()
    await ctx.relay.handle_frame(encode_frame(KIND_JPEG, JPEG, 5), ctx.manager)
    runner = _stt_runner("目の前に何がありますか")
    runner.heard_hook = ctx.qa.on_heard
    ctx.qa.start()
    try:
        await _speak_into_media(ctx, runner)
        for _ in range(100):
            if not voice_in or any(m["type"] == "vlm_answer" for m in sent(app)):
                break
            await asyncio.sleep(0.01)
    finally:
        await ctx.qa.stop()
    heard = [m for m in sent(app) if m["type"] == "heard"]
    assert heard == [{"type": "heard", "text": "目の前に何がありますか", "intent": intent}]
    queries = [m for m in sent(app) if m["type"] == "vlm_query"]
    answers = [m for m in sent(app) if m["type"] == "vlm_answer"]
    if voice_in:
        assert [(q["source"], q["question"]) for q in queries] == [("voice", "目の前に何がありますか")]
        assert answers[0]["answer"] == "コップです。"
        assert answers[0]["source"] == "voice"
    else:
        assert queries == [] and answers == []


# --- WS: greeting と vlm_ask -------------------------------------------------------------


async def test_app_greeting_includes_vlm_states():
    ctx = Ctx(voice_in=True)
    ctx.qa.voice.output = False
    await ctx.vlm.refresh(force=True)
    ws = make_ws()
    await websocket_endpoint(
        ws, "app", ctx.manager, ctx.listen_state, ctx.anim_state, ctx.drive, ctx.relay, vlm_qa=ctx.qa
    )
    values = states(ws)
    assert "vlm:ready" in values
    assert "vlm_voice_in:on" in values
    assert "vlm_voice_out:off" in values
    # greeting の後ろに付く。
    assert values.index("vlm:ready") > values.index("stt:off")


async def test_ws_vlm_ask_from_app_submits_text_question():
    ctx = Ctx()
    ctx.qa.submit = AsyncMock(return_value=1)
    ws = make_ws([{"type": "websocket.text", "text": json.dumps({"type": "vlm_ask", "question": "これは何?"})}])
    await websocket_endpoint(
        ws, "app", ctx.manager, ctx.listen_state, ctx.anim_state, ctx.drive, ctx.relay, vlm_qa=ctx.qa
    )
    ctx.qa.submit.assert_awaited_once_with("これは何?", "text")


async def test_ws_vlm_ask_from_device_is_ignored():
    ctx = Ctx()
    ctx.qa.submit = AsyncMock(return_value=1)
    ws = make_ws([{"type": "websocket.text", "text": json.dumps({"type": "vlm_ask", "question": "q"})}])
    await websocket_endpoint(
        ws, "device", ctx.manager, ctx.listen_state, ctx.anim_state, ctx.drive, ctx.relay, vlm_qa=ctx.qa
    )
    ctx.qa.submit.assert_not_awaited()


async def test_ws_control_vlm_voice_from_app():
    ctx = Ctx(listen=False)
    control = {"type": "control", "command": "vlm_voice", "params": {"input": True, "output": True}}
    ws = make_ws([{"type": "websocket.text", "text": json.dumps(control)}])
    await websocket_endpoint(
        ws, "app", ctx.manager, ctx.listen_state, ctx.anim_state, ctx.drive, ctx.relay, vlm_qa=ctx.qa
    )
    assert (ctx.qa.voice.input, ctx.qa.voice.output) == (True, True)
    assert ctx.listen_state.on is True
