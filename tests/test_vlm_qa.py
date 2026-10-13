"""質問応答 (``VlmQaService``) のテスト。

ダミー VLM (``VlmService`` + ダミーバックエンド) とダミー SpeechQueue を使う。
"""

import asyncio
import json
import logging
from unittest.mock import AsyncMock, MagicMock

import pytest

from openmovehub.config import DEFAULT_VLM_QA_SYSTEM_PROMPT, Settings
from openmovehub.models.frames import KIND_JPEG, encode_frame
from openmovehub.models.messages import VlmAnswerMessage, VlmQueryMessage, parse_message
from openmovehub.services.camera_relay import CameraRelay
from openmovehub.services.connection_manager import ConnectionManager
from openmovehub.services.vlm import VlmResult, VlmService, VlmTimeout, VlmUnavailable
from openmovehub.services.vlm_qa import ERROR_SPEECH, VlmQaService, VlmVoiceState, clean_answer, log_vlm_state

JPEG = b"\xff\xd8\xff\xe0" + b"photo" * 8


class FakeBackend:
    name = "fake"
    url = "http://fake"
    model = "fake:1b"

    def __init__(self, text: str = "机の上にコップがあります。") -> None:
        self.text = text
        self.fail: Exception | None = None
        self.gate: asyncio.Event | None = None
        self.probe_ok = True
        self.calls: list[dict] = []

    async def probe(self) -> dict:
        if self.probe_ok:
            return {"ok": True, "version": "0.33.1", "model_present": True, "message": "ok"}
        return {"ok": False, "version": None, "model_present": False, "message": "ollama unreachable"}

    async def chat(self, images, system, user, schema, options) -> VlmResult:
        self.calls.append({"images": images, "system": system, "user": user, "schema": schema, "options": options})
        if self.gate is not None:
            await self.gate.wait()
        if self.fail is not None:
            raise self.fail
        return VlmResult(text=self.text, data=None, ms=42.0, load_ms=0.0, prompt_tokens=1, eval_tokens=1, model="m")

    async def aclose(self) -> None:
        return None


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def make_app_ws() -> MagicMock:
    ws = MagicMock()
    ws.accept = AsyncMock()
    ws.send_text = AsyncMock()
    ws.send_bytes = AsyncMock()
    return ws


def sent(ws: MagicMock) -> list[dict]:
    return [json.loads(c.args[0]) for c in ws.send_text.await_args_list]


def make_speech_queue() -> MagicMock:
    sq = MagicMock()
    sq.enqueue = AsyncMock()
    return sq


class Env:
    """``VlmQaService`` とその周り (app 1 つ・カメラ・ダミー SpeechQueue) をまとめたもの。"""

    def __init__(self, backend: FakeBackend, qa: VlmQaService, relay: CameraRelay, clock: Clock, app_ws, sq) -> None:
        self.backend = backend
        self.qa = qa
        self.relay = relay
        self.clock = clock
        self.app_ws = app_ws
        self.speech_queue = sq

    async def feed_frame(self) -> None:
        await self.relay.handle_frame(encode_frame(KIND_JPEG, JPEG, 5), self.qa._manager)

    def messages(self, type_: str | None = None) -> list[dict]:
        msgs = [m for m in sent(self.app_ws) if m["type"] in ("vlm_query", "vlm_answer", "state")]
        return [m for m in msgs if type_ is None or m["type"] == type_]


@pytest.fixture
async def env():
    created: list[VlmQaService] = []

    async def _make(
        *,
        backend: FakeBackend | None = None,
        output: bool = False,
        tts_ok: bool = True,
        frame: bool = True,
        **settings_kwargs,
    ) -> Env:
        backend = backend or FakeBackend()
        settings = Settings(**settings_kwargs)
        manager = ConnectionManager()
        app_ws = make_app_ws()
        await manager.connect(app_ws, "app")
        clock = Clock()
        relay = CameraRelay(clock=clock)
        vlm = VlmService(settings, backend=backend)
        await vlm.refresh(force=True)
        sq = make_speech_queue()
        qa = VlmQaService(
            vlm,
            manager,
            relay,
            settings,
            speech_queue=sq,
            tts_available=lambda: tts_ok,
            stt_state=lambda: "ready",
            voice=VlmVoiceState(input=False, output=output),
        )
        qa.start()
        created.append(qa)
        e = Env(backend, qa, relay, clock, app_ws, sq)
        if frame:
            await e.feed_frame()
        return e

    yield _make
    for qa in created:
        await qa.stop()


# --- 回答の整形 -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("  コップがあります。  ", "コップがあります。"),
        ("**赤い**コップがあります。", "赤いコップがあります。"),
        ("## 見えるもの\n- コップ\n- 本", "見えるもの\nコップ\n本"),
        ("`USB` ケーブルです。", "USB ケーブルです。"),
        ("値段は 3-4 円 - たぶん", "値段は 3-4 円 - たぶん"),  # 行頭以外の "- " は残す
    ],
)
def test_clean_answer_removes_markdown(raw, expected):
    assert clean_answer(raw) == expected


def test_default_system_prompt_is_the_adr_text():
    assert DEFAULT_VLM_QA_SYSTEM_PROMPT == (
        "あなたは小型ロボットの目です。添付画像はロボットのカメラが今写している映像です。"
        "質問に、画像に写っているものだけを根拠に日本語で答えてください。"
        "音声で読み上げられるので、2〜3 文以内の話し言葉で、箇条書きや記号を使わずに答えてください。"
        "画像から分からないことは分からないと答えてください。"
    )


def test_messages_parse():
    assert parse_message(json.dumps({"type": "vlm_ask", "question": "q"})).question == "q"
    query = parse_message(json.dumps({"type": "vlm_query", "id": 1, "question": "q", "source": "voice", "ts": 1.0}))
    assert isinstance(query, VlmQueryMessage)
    answer = parse_message(
        json.dumps(
            {
                "type": "vlm_answer",
                "id": 1,
                "question": "q",
                "answer": "a",
                "source": "text",
                "ms": None,
                "frame_seq": None,
                "spoken": False,
                "error": "no_frame",
            }
        )
    )
    assert isinstance(answer, VlmAnswerMessage)


# --- 1 件の処理 ---------------------------------------------------------------------


async def test_text_question_sends_query_then_answer(env):
    e = await env()
    answer = await e.qa.ask("何が見えますか?", "text")

    msgs = e.messages()
    assert [m["type"] for m in msgs] == ["vlm_query", "vlm_answer"]
    query, sent_answer = msgs
    assert query["id"] == 1
    assert query["question"] == "何が見えますか?"
    assert query["source"] == "text"
    assert isinstance(query["ts"], float)
    assert sent_answer == {
        "type": "vlm_answer",
        "id": 1,
        "question": "何が見えますか?",
        "answer": "机の上にコップがあります。",
        "source": "text",
        "ms": 42.0,
        "frame_seq": 1,
        "spoken": False,
        "error": None,
    }
    assert answer.model_dump() == sent_answer
    # 最新フレーム 1 枚・既定のシステムプロンプト・schema 無し・max_tokens = VLM_NUM_PREDICT。
    call = e.backend.calls[0]
    assert call["images"] == [JPEG]
    assert call["system"] == DEFAULT_VLM_QA_SYSTEM_PROMPT
    assert call["user"] == "何が見えますか?"
    assert call["schema"] is None
    assert call["options"] == {"num_predict": 400}
    assert e.qa.frame(1) == JPEG


async def test_question_is_stripped(env):
    e = await env()
    answer = await e.qa.ask("  何?  ", "text")
    assert answer.question == "何?"
    assert e.backend.calls[0]["user"] == "何?"


async def test_answer_markdown_is_removed(env):
    e = await env(backend=FakeBackend(text="**コップ**があります。\n- 白い"))
    answer = await e.qa.ask("q", "text")
    assert answer.answer == "コップがあります。\n白い"


async def test_submit_returns_increasing_ids(env):
    e = await env()
    first = await e.qa.submit("1", "text")
    second = await e.qa.submit("2", "voice")
    assert (first, second) == (1, 2)
    for _ in range(100):
        if len(e.messages("vlm_answer")) == 2:
            break
        await asyncio.sleep(0.01)
    answers = e.messages("vlm_answer")
    assert [(a["id"], a["source"]) for a in answers] == [(1, "text"), (2, "voice")]


async def test_no_frame_without_camera(env):
    e = await env(frame=False)
    answer = await e.qa.ask("q", "text")
    assert answer.error == "no_frame"
    assert answer.frame_seq is None
    assert answer.ms is None
    assert e.backend.calls == []
    assert [m["type"] for m in e.messages()] == ["vlm_query", "vlm_answer"]


async def test_no_frame_when_frame_is_stale(env):
    e = await env()
    e.clock.now += 2.5  # VLM_QA_FRAME_MAX_AGE_S (2.0 s) より古い
    answer = await e.qa.ask("q", "text")
    assert answer.error == "no_frame"
    assert e.backend.calls == []
    assert e.qa.frame(1) is None


async def test_frame_within_max_age_is_used(env):
    e = await env()
    e.clock.now += 1.9
    answer = await e.qa.ask("q", "text")
    assert answer.error is None


@pytest.mark.parametrize(
    ("fail", "error"),
    [(VlmUnavailable("ollama unreachable"), "unavailable"), (VlmTimeout("timed out"), "timeout")],
)
async def test_vlm_errors(env, fail, error):
    backend = FakeBackend()
    backend.fail = fail
    e = await env(backend=backend)
    answer = await e.qa.ask("q", "text")
    assert answer.error == error
    assert answer.answer == ""
    assert answer.frame_seq == 1  # 見ようとした画像は残す
    assert e.qa.frame(1) == JPEG


@pytest.mark.parametrize("question", ["", "   ", "あ" * 501])
async def test_invalid_question_is_rejected(env, question):
    e = await env()
    answer = await e.qa.ask(question, "text")
    assert answer.error == "invalid"
    assert e.backend.calls == []
    assert [m["type"] for m in e.messages()] == ["vlm_answer"]


async def test_500_chars_is_accepted(env):
    e = await env()
    answer = await e.qa.ask("あ" * 500, "text")
    assert answer.error is None


async def test_queue_limit_returns_busy(env):
    backend = FakeBackend()
    backend.gate = asyncio.Event()
    e = await env(backend=backend, vlm_qa_queue_max=3)
    await e.qa.submit("processing", "text")
    await asyncio.sleep(0.02)  # 1 件目はワーカーが取り出して処理中
    for i in range(3):
        await e.qa.submit(f"waiting {i}", "text")
    assert e.qa.queue_size == 3
    assert e.qa.status()["queue"] == 3
    busy = await e.qa.ask("overflow", "text")
    assert busy.error == "busy"
    assert busy.id == 5
    backend.gate.set()
    for _ in range(100):
        if len(e.messages("vlm_answer")) == 5:
            break
        await asyncio.sleep(0.01)
    errors = [a["error"] for a in e.messages("vlm_answer")]
    assert errors.count("busy") == 1
    assert errors.count(None) == 4


async def test_frame_ring_keeps_last_n(env):
    e = await env(vlm_qa_frame_keep=2)
    for _ in range(3):
        await e.qa.ask("q", "text")
    assert e.qa.frame(1) is None
    assert e.qa.frame(2) == JPEG
    assert e.qa.frame(3) == JPEG


# --- 音声出力 -----------------------------------------------------------------------


async def test_voice_output_on_enqueues_answer(env):
    e = await env(output=True)
    answer = await e.qa.ask("q", "text")
    assert answer.spoken is True
    e.speech_queue.enqueue.assert_awaited_once_with("机の上にコップがあります。")


async def test_voice_output_off_does_not_enqueue(env):
    e = await env(output=False)
    answer = await e.qa.ask("q", "voice")
    assert answer.spoken is False
    e.speech_queue.enqueue.assert_not_awaited()


async def test_tts_unavailable_skips_speech_and_spoken_false(env):
    e = await env(output=True, tts_ok=False)
    answer = await e.qa.ask("q", "text")
    assert answer.spoken is False
    assert answer.answer
    e.speech_queue.enqueue.assert_not_awaited()


async def test_spoken_false_without_speech_queue():
    settings = Settings()
    manager = ConnectionManager()
    relay = CameraRelay()
    vlm = VlmService(settings, backend=FakeBackend())
    qa = VlmQaService(vlm, manager, relay, settings, voice=VlmVoiceState(output=True))
    assert qa.tts_ok() is False


@pytest.mark.parametrize(
    ("fail", "frame", "error"),
    [
        (None, False, "no_frame"),
        (VlmUnavailable("down"), True, "unavailable"),
        (VlmTimeout("slow"), True, "timeout"),
    ],
)
async def test_error_phrase_is_spoken(env, fail, frame, error):
    backend = FakeBackend()
    backend.fail = fail
    e = await env(backend=backend, output=True, frame=frame)
    answer = await e.qa.ask("q", "text")
    assert answer.error == error
    assert answer.spoken is True
    e.speech_queue.enqueue.assert_awaited_once_with(ERROR_SPEECH[error])


async def test_error_phrases_are_the_adr_text():
    assert ERROR_SPEECH == {
        "no_frame": "カメラの画像がありません",
        "unavailable": "VLM に接続できません",
        "timeout": "時間内に答えられませんでした",
    }


async def test_busy_and_invalid_are_not_spoken(env):
    e = await env(output=True)
    answer = await e.qa.ask("", "text")
    assert answer.error == "invalid"
    assert answer.spoken is False
    e.speech_queue.enqueue.assert_not_awaited()


async def test_answer_is_sent_before_speech(env):
    e = await env(output=True)
    order: list[str] = []
    e.app_ws.send_text.side_effect = lambda text: order.append(json.loads(text)["type"])
    e.speech_queue.enqueue.side_effect = lambda text: order.append("enqueue")
    await e.qa.ask("q", "text")
    assert order == ["vlm_query", "vlm_answer", "enqueue"]


# --- 可用性 (state vlm:*) -------------------------------------------------------------


async def test_refresh_state_broadcasts_only_on_change(env):
    e = await env()
    assert e.qa.vlm_state == "ready"
    await e.qa.refresh_state()
    assert e.messages("state") == []
    e.backend.probe_ok = False
    assert await e.qa.refresh_state() == "unavailable"
    assert e.messages("state") == [{"type": "state", "value": "vlm:unavailable"}]
    await e.qa.refresh_state()
    assert len(e.messages("state")) == 1
    e.backend.probe_ok = True
    await e.qa.refresh_state()
    assert e.messages("state")[-1] == {"type": "state", "value": "vlm:ready"}


async def test_monitor_loop_refreshes_periodically(env):
    e = await env(vlm_refresh_interval_s=0.02)
    e.backend.probe_ok = False
    for _ in range(100):
        if {"type": "state", "value": "vlm:unavailable"} in e.messages("state"):
            break
        await asyncio.sleep(0.01)
    assert {"type": "state", "value": "vlm:unavailable"} in e.messages("state")


async def test_backend_none_is_off_and_answers_unavailable():
    settings = Settings(vlm_backend="none")
    manager = ConnectionManager()
    app_ws = make_app_ws()
    await manager.connect(app_ws, "app")
    relay = CameraRelay()
    await relay.handle_frame(encode_frame(KIND_JPEG, JPEG, 5), manager)
    qa = VlmQaService(VlmService(settings), manager, relay, settings)
    qa.start()
    try:
        assert qa.vlm_state == "off"
        assert qa.status()["state"] == "off"
        answer = await qa.ask("q", "text")
        assert answer.error == "unavailable"
    finally:
        await qa.stop()


async def test_status_shape(env):
    e = await env(output=True)
    status = e.qa.status()
    assert {"backend", "model", "available", "version", "message", "state", "voice", "queue"} <= set(status)
    assert status["voice"] == {"input": False, "output": True}
    assert status["queue"] == 0


async def test_log_vlm_state_messages(caplog):
    caplog.set_level(logging.INFO, logger="openmovehub.services.vlm_qa")
    vlm = VlmService(Settings(), backend=FakeBackend())
    await vlm.refresh(force=True)
    log_vlm_state(vlm)
    assert "VLM ready (backend=fake model=fake:1b version=0.33.1)" in caplog.text
    log_vlm_state(VlmService(Settings(vlm_backend="none")))
    assert "VLM disabled" in caplog.text


async def test_unexpected_error_still_broadcasts_answer(env):
    """想定外の例外でも vlm_answer{error:"unavailable"} を配信し、Web の「考え中…」を残さない。"""
    e = await env()
    e.backend.fail = RuntimeError("boom")
    answer = await e.qa.ask("何が見えますか?", "text")

    assert answer.error == "unavailable"
    assert [m["type"] for m in e.messages()] == ["vlm_query", "vlm_answer"]
    assert e.messages("vlm_answer")[0]["error"] == "unavailable"
