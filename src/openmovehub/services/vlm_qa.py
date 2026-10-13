"""VLM 質問応答 (``VlmQaService``) と音声の入出力の切替 (``VlmVoiceState``)。

- 質問 (WS ``vlm_ask`` / REST ``POST /vlm/qa`` / 音声入力 ON 時の認識結果) をキューに積み、1 件ずつ処理する。
  1 件の処理: 全 app に ``vlm_query`` → ``CameraRelay.latest`` (2 s 以内) を 1 枚だけ添えて ``VlmService.ask``
  → 回答の Markdown 記号を除く → 全 app に ``vlm_answer`` → 音声出力 ON なら ``SpeechQueue`` で読み上げ。
- 会話の履歴は持たない (1 問 1 答。毎回そのときの画像だけを見る)。
- どの画像を見て答えたかを Web に出すため、使ったフレームを質問 ID ごとにリングで保持する。
- 音声の入力・出力は Hub 全体で単一の状態 (last-write-wins)。変化したら ``state vlm_voice_in|out:on|off``。
- 可用性 (``VlmService.refresh``) を一定間隔 (既定 30 s) で確かめ、変化したら ``state vlm:ready|unavailable|off``。
"""

import asyncio
import logging
import re
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal

from openmovehub.models.messages import StateMessage, VlmAnswerMessage, VlmQueryMessage, VlmSource
from openmovehub.services.vlm import VlmError, VlmService, VlmTimeout

logger = logging.getLogger(__name__)

VlmState = Literal["ready", "unavailable", "off"]

#: 音声出力 ON のときにエラーの代わりに読み上げる定型文 (``busy`` / ``invalid`` は読み上げない)。
ERROR_SPEECH: dict[str, str] = {
    "no_frame": "カメラの画像がありません",
    "unavailable": "VLM に接続できません",
    "timeout": "時間内に答えられませんでした",
}

#: STT がこの状態のとき音声入力 ON を拒否する (``loading`` は受け付ける)。
STT_REJECT_STATES = ("off", "error")

_LIST_MARK_RE = re.compile(r"^\s*- ", re.MULTILINE)


def clean_answer(text: str) -> str:
    """前後の空白と Markdown の記号 (``**``・``#``・行頭の ``- ``・`` ` ``) を除く。

    音声で読み上げたときに記号が読まれないようにするため。
    """
    text = text.replace("**", "").replace("`", "").replace("#", "")
    text = _LIST_MARK_RE.sub("", text)
    lines = [line.strip() for line in text.strip().splitlines()]
    return "\n".join(line for line in lines if line)


def vlm_state_of(vlm: VlmService) -> VlmState:
    """``ready`` = Ollama 応答あり・版 ≥ 最小・モデルあり。バックエンド無し (``VLM_BACKEND=none``) は ``off``。"""
    if vlm.backend is None:
        return "off"
    return "ready" if vlm.available else "unavailable"


def log_vlm_state(vlm: VlmService) -> None:
    """起動時と状態変化時のログ (``VLM ready (backend=ollama model=... version=...)``)。"""
    state = vlm_state_of(vlm)
    status = vlm.status()
    if state == "off":
        logger.info("VLM disabled (VLM_BACKEND=none)")
    elif state == "ready":
        logger.info("VLM ready (backend=%s model=%s version=%s)", status["backend"], status["model"], status["version"])
    else:
        logger.warning(
            "VLM unavailable (backend=%s model=%s version=%s): %s",
            status["backend"],
            status["model"],
            status["version"],
            status["message"],
        )


def _on_off(value: bool) -> str:
    return "on" if value else "off"


@dataclass
class VlmVoiceState:
    """音声の入力 (認識結果を質問にする)・出力 (回答を読み上げる)。Hub 全体で 1 つ。"""

    input: bool = False
    output: bool = False


@dataclass
class _Item:
    id: int
    question: str
    source: VlmSource
    future: asyncio.Future


class VlmQaService:
    def __init__(
        self,
        vlm: VlmService,
        manager: Any,
        camera_relay: Any,
        settings: Any,
        *,
        speech_queue: Any | None = None,
        tts_available: Callable[[], bool] | None = None,
        stt_state: Callable[[], str] | None = None,
        voice: VlmVoiceState | None = None,
    ) -> None:
        self.vlm = vlm
        self._manager = manager
        self._camera_relay = camera_relay
        self._settings = settings
        self._speech_queue = speech_queue
        self._tts_available = tts_available
        self._stt_state = stt_state
        self.voice = (
            voice
            if voice is not None
            else VlmVoiceState(input=settings.vlm_voice_input_default, output=settings.vlm_voice_output_default)
        )
        self._queue: asyncio.Queue[_Item] = asyncio.Queue()
        self._next_id = 1
        self._frames: OrderedDict[int, bytes] = OrderedDict()
        self._last_state: VlmState = vlm_state_of(vlm)
        self._worker_task: asyncio.Task | None = None
        self._monitor_task: asyncio.Task | None = None

    # --- 状態 ---------------------------------------------------------------

    @property
    def vlm_state(self) -> VlmState:
        return vlm_state_of(self.vlm)

    @property
    def stt_state(self) -> str:
        return self._stt_state() if self._stt_state is not None else "off"

    @property
    def queue_size(self) -> int:
        """処理待ちの質問数 (処理中の 1 件は含まない)。"""
        return self._queue.qsize()

    def tts_ok(self) -> bool:
        """読み上げられるか (``TTS_PLUGIN=none`` や VOICEVOX 未起動なら False)。"""
        if self._speech_queue is None:
            return False
        if self._tts_available is None:
            return True
        try:
            return bool(self._tts_available())
        except Exception:
            logger.exception("tts_available() failed")
            return False

    def status(self) -> dict[str, Any]:
        """``GET /vlm`` と ``/status.vlm``。``VlmService.status()`` + ``state``・``voice``・``queue``。"""
        return {
            **self.vlm.status(),
            "state": self.vlm_state,
            "voice": {"input": self.voice.input, "output": self.voice.output},
            "queue": self.queue_size,
        }

    def state_messages(self) -> list[StateMessage]:
        """app の greeting に含める ``state`` (``vlm:*``・``vlm_voice_in:*``・``vlm_voice_out:*``)。"""
        return [
            StateMessage(type="state", value=f"vlm:{self.vlm_state}"),
            StateMessage(type="state", value=f"vlm_voice_in:{_on_off(self.voice.input)}"),
            StateMessage(type="state", value=f"vlm_voice_out:{_on_off(self.voice.output)}"),
        ]

    async def _send_state(self, value: str) -> None:
        await self._manager.send_json_to_apps(StateMessage(type="state", value=value))

    # --- 音声の入出力の切替 (§3) ---------------------------------------------

    async def set_voice_output(self, on: bool) -> None:
        on = bool(on)
        if on == self.voice.output:
            return
        self.voice.output = on
        logger.info("VLM voice output set to %s", on)
        await self._send_state(f"vlm_voice_out:{_on_off(on)}")

    async def set_voice_input(self, on: bool) -> bool:
        """音声入力を切り替える。STT が ``off`` / ``error`` のときの ON は拒否して ``state vlm_voice_in:off`` を返す。

        受け付けたら True。listen との連動 (ON で listen も ON) は呼び出し側 (``control vlm_voice``) が行う。
        """
        on = bool(on)
        if on and self.stt_state in STT_REJECT_STATES:
            logger.warning("VLM voice input rejected (stt=%s)", self.stt_state)
            self.voice.input = False
            await self._send_state("vlm_voice_in:off")
            return False
        if on != self.voice.input:
            self.voice.input = on
            logger.info("VLM voice input set to %s", on)
            await self._send_state(f"vlm_voice_in:{_on_off(on)}")
        return True

    async def on_heard(self, text: str) -> str:
        """heard フック。音声入力 ON なら認識結果を質問として積み、intent ``"vlm"`` を返す。"""
        if not self.voice.input:
            return ""
        await self.submit(text, "voice")
        return "vlm"

    # --- 質問 ---------------------------------------------------------------

    def _new_item(self, question: str, source: VlmSource) -> _Item:
        item = _Item(
            id=self._next_id,
            question=question,
            source=source,
            future=asyncio.get_running_loop().create_future(),
        )
        self._next_id += 1
        return item

    async def _enqueue(self, question: str, source: VlmSource) -> _Item:
        question = str(question).strip()
        item = self._new_item(question, source)
        error: str | None = None
        if not question or len(question) > self._settings.vlm_qa_max_question_chars:
            error = "invalid"
        elif self._queue.qsize() >= self._settings.vlm_qa_queue_max:
            error = "busy"
        if error is not None:
            logger.warning("VLM question #%d rejected (%s): %s", item.id, error, question[:50])
            msg = self._answer(item, answer="", ms=None, frame_seq=None, spoken=False, error=error)
            await self._manager.send_json_to_apps(msg)
            item.future.set_result(msg)
            return item
        await self._queue.put(item)
        return item

    async def submit(self, question: str, source: VlmSource) -> int:
        """質問をキューに積んで ID を返す (回答は ``vlm_answer`` で全 app に届く)。"""
        item = await self._enqueue(question, source)
        return item.id

    async def ask(self, question: str, source: VlmSource = "text") -> VlmAnswerMessage:
        """``submit`` して回答が出るまで待つ (``POST /vlm/qa`` 用。app への配信も行う)。"""
        item = await self._enqueue(question, source)
        return await item.future

    def frame(self, qid: int) -> bytes | None:
        """その質問で使った JPEG (リングから消えていれば None)。"""
        return self._frames.get(qid)

    def _store_frame(self, qid: int, jpeg: bytes) -> None:
        self._frames[qid] = jpeg
        while len(self._frames) > max(1, self._settings.vlm_qa_frame_keep):
            self._frames.popitem(last=False)

    @staticmethod
    def _answer(
        item: _Item, *, answer: str, ms: float | None, frame_seq: int | None, spoken: bool, error: str | None
    ) -> VlmAnswerMessage:
        return VlmAnswerMessage(
            type="vlm_answer",
            id=item.id,
            question=item.question,
            answer=answer,
            source=item.source,
            ms=ms,
            frame_seq=frame_seq,
            spoken=spoken,
            error=error,
        )

    async def _process(self, item: _Item) -> VlmAnswerMessage:
        # 1. 質問を全 app に出す (音声で聞いた質問も)。
        await self._manager.send_json_to_apps(
            VlmQueryMessage(type="vlm_query", id=item.id, question=item.question, source=item.source, ts=time.time())
        )
        answer = ""
        ms: float | None = None
        frame_seq: int | None = None
        error: str | None = None

        # 2. 最新フレーム (古い画像では答えさせない)。
        latest = self._camera_relay.latest
        age = self._camera_relay.last_frame_age_s
        if latest is None or age is None or age > self._settings.vlm_qa_frame_max_age_s:
            error = "no_frame"
        else:
            jpeg = latest["jpeg"]
            frame_seq = int(latest["seq"])
            # 3. どの画像を見て答えたかを Web に出すために保持する。
            self._store_frame(item.id, jpeg)
            # 4. 問い合わせ。
            try:
                result = await self.vlm.ask(
                    [jpeg],
                    system=self._settings.vlm_qa_system_prompt,
                    user=item.question,
                    schema=None,
                    max_tokens=self._settings.vlm_num_predict,
                )
            except VlmTimeout:
                error = "timeout"
            except VlmError:
                error = "unavailable"
            else:
                # 5. 読み上げで記号が読まれないように整える。
                answer = clean_answer(result.text)
                ms = result.ms

        # 7 の準備: 音声出力 ON なら回答 (エラー時は定型文) を読み上げる。TTS が使えなければ読み上げない。
        speech = ERROR_SPEECH.get(error, "") if error is not None else answer
        spoken = bool(self.voice.output and speech and self.tts_ok())

        # 6. 全 app に回答を出す。
        msg = self._answer(item, answer=answer, ms=ms, frame_seq=frame_seq, spoken=spoken, error=error)
        await self._manager.send_json_to_apps(msg)
        if error is None:
            logger.info("VLM answer #%d (%s, %.0f ms): %s", item.id, item.source, ms or 0.0, answer[:100])
        else:
            logger.warning("VLM answer #%d (%s) error=%s", item.id, item.source, error)

        # 7. 読み上げ。
        if spoken:
            await self._speech_queue.enqueue(speech)
        return msg

    async def _worker(self) -> None:
        while True:
            item = await self._queue.get()
            try:
                msg = await self._process(item)
            except Exception:
                logger.exception("VLM question #%d failed", item.id)
                msg = self._answer(item, answer="", ms=None, frame_seq=None, spoken=False, error="unavailable")
                # vlm_query を出した後なので、Web の「考え中…」を残さないよう回答 (エラー) も配信する。
                try:
                    await self._manager.send_json_to_apps(msg)
                except Exception:
                    logger.exception("VLM answer #%d broadcast failed", item.id)
            finally:
                self._queue.task_done()
            if not item.future.done():
                item.future.set_result(msg)

    # --- 可用性の監視 (§4) ----------------------------------------------------

    async def refresh_state(self) -> VlmState:
        """``VlmService.refresh()`` を行い、状態が変わったらログと ``state vlm:*`` を出す。"""
        if self.vlm.backend is not None:
            await self.vlm.refresh(force=True)
        state = self.vlm_state
        if state != self._last_state:
            self._last_state = state
            log_vlm_state(self.vlm)
            await self._send_state(f"vlm:{state}")
        return state

    async def _monitor(self) -> None:
        while True:
            await asyncio.sleep(self._settings.vlm_refresh_interval_s)
            try:
                await self.refresh_state()
            except Exception:
                logger.exception("VLM refresh failed")

    # --- 起動・停止 -----------------------------------------------------------

    def start(self) -> None:
        self._last_state = self.vlm_state
        self._worker_task = asyncio.create_task(self._worker())
        if self.vlm.backend is not None:
            self._monitor_task = asyncio.create_task(self._monitor())

    async def stop(self) -> None:
        for task in (self._worker_task, self._monitor_task):
            if task is None:
                continue
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        self._worker_task = None
        self._monitor_task = None
